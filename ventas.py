"""
CeVi comercial (ElevenLabs) — página pública app.c4vlaser.com/ventas
=====================================================================
Mismo personaje que el CeVi de soporte, pero para personas que todavía NO son
clientes: presenta las máquinas, diagnostica qué necesita cada una y, cuando
hay interés de compra, deja el lead en Odoo CRM para que una asesora lo siga.

Sin login no hay identidad, así que:
- /voz/url-publica da URLs firmadas a cualquiera que venga del portal, con
  tope por visitante (IP) y tope global: el agente tiene la autenticación de
  ElevenLabs activada, y sin este endpoint nadie puede hablarle (así el
  agent_id no sirve para abrir conversaciones desde otro sitio).
- /webhook/registrar-interesado lo llama el agente (tool) con los datos que
  la persona DICTA: nombre y WhatsApp son suyos, no hay nada que proteger de
  un tercero. El secreto X-CeVi-Secret asegura que solo ElevenLabs lo llame.

Variables: ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID_VENTAS, CEVI_WEBHOOK_SECRET,
VENTAS_ORIGENES (opcional, coma: orígenes extra permitidos).
"""
import hashlib
import hmac
import logging
import os
import re
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import conversaciones
import odoo_client

log = logging.getLogger("cevi.ventas")
router = APIRouter()

AGENTE_VENTAS = os.environ.get("ELEVENLABS_AGENT_ID_VENTAS", "")
ORIGENES = {"https://app.c4vlaser.com", "https://sebastianjcv97.github.io"} | {
    o.strip().rstrip("/") for o in os.environ.get("VENTAS_ORIGENES", "").split(",") if o.strip()}

# Topes: cada visita pide ~3 URLs (una la gasta el widget al leer su
# configuración y mantiene 2 de reserva) y una más por conversación.
POR_IP_MAX, POR_IP_VENTANA = 24, 600          # 24 URLs cada 10 min por visitante
GLOBAL_MAX, GLOBAL_VENTANA = 600, 3600        # 600 por hora entre todos
_por_ip: dict = {}
_global: list = []


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "?")


def _excede(ip: str) -> bool:
    ahora = time.time()
    global _global
    _global = [t for t in _global if ahora - t < GLOBAL_VENTANA]
    marcas = [t for t in _por_ip.get(ip, []) if ahora - t < POR_IP_VENTANA]
    if len(marcas) >= POR_IP_MAX or len(_global) >= GLOBAL_MAX:
        _por_ip[ip] = marcas
        return True
    marcas.append(ahora)
    _por_ip[ip] = marcas
    _global.append(ahora)
    if len(_por_ip) > 5000:                       # que el diccionario no crezca sin fin
        for k in [k for k, v in _por_ip.items() if not v or ahora - v[-1] > POR_IP_VENTANA]:
            _por_ip.pop(k, None)
    return False


@router.post("/voz/url-publica")
async def voz_url_publica(request: Request):
    origen = (request.headers.get("origin") or "").rstrip("/")
    if origen not in ORIGENES:
        return JSONResponse({"error": "origen no permitido"}, status_code=403)
    if not AGENTE_VENTAS:
        return JSONResponse({"error": "CeVi comercial no configurado"}, status_code=503)
    if _excede(_ip(request)):
        return JSONResponse({"error": "demasiadas solicitudes"}, status_code=429)
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return JSONResponse({"error": "voz no configurada"}, status_code=503)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
                                 params={"agent_id": AGENTE_VENTAS, "include_conversation_id": "true"},
                                 headers={"xi-api-key": key})
    except httpx.HTTPError as e:
        log.error("get-signed-url (ventas) sin respuesta: %s", e)
        return JSONResponse({"error": "no se pudo iniciar la voz"}, status_code=502)
    if r.status_code != 200:
        log.error("get-signed-url (ventas) HTTP %s: %s", r.status_code, r.text[:200])
        return JSONResponse({"error": "no se pudo iniciar la voz"}, status_code=502)
    url = r.json().get("signed_url")
    try:
        from urllib.parse import urlparse, parse_qs
        conv = (parse_qs(urlparse(url).query).get("conversation_id") or [None])[0]
        if conv:
            await run_in_threadpool(conversaciones.registrar_emision, conv, None, None, None, "ventas")
    except Exception:
        log.exception("No se pudo registrar la conversación (ventas)")
    return {"signed_url": url}


# ── Tool: registrar_interesado ──────────────────────────────────────────────
PREFIJOS = {"PE": "51", "EC": "593", "BO": "591", "CL": "56", "CO": "57"}
_PAIS_NOMBRE = {"peru": "PE", "perú": "PE", "ecuador": "EC", "bolivia": "BO", "chile": "CL", "colombia": "CO"}


def _pais_codigo(pais: Optional[str]) -> Optional[str]:
    p = (pais or "").strip().lower()
    if not p:
        return None
    if p.upper() in PREFIJOS:
        return p.upper()
    return _PAIS_NOMBRE.get(p)


def normalizar_telefono(telefono: str, pais: Optional[str]) -> Optional[str]:
    """'+51 995 547 575', '995547575' (PE) → '+51995547575'. None si no parece un número."""
    t = (telefono or "").strip()
    dig = re.sub(r"\D", "", t)
    if len(dig) < 7:
        return None
    if t.startswith("+") or t.startswith("00"):
        return "+" + dig.lstrip("0") if t.startswith("00") else "+" + dig
    pref = PREFIJOS.get(_pais_codigo(pais) or "PE")
    if dig.startswith(pref) and len(dig) > len(pref) + 7:
        return "+" + dig
    return "+" + pref + dig.lstrip("0")


def _autorizado(secret_header: Optional[str]) -> bool:
    esperado = os.environ.get("CEVI_WEBHOOK_SECRET")
    if not esperado:
        log.error("CEVI_WEBHOOK_SECRET no configurada — webhooks de ventas CERRADOS")
        return False
    return bool(secret_header) and hmac.compare_digest(secret_header, esperado)


_RECIENTES: dict = {}   # teléfono → (momento, lead_id): no duplicar en 10 min


class InteresadoReq(BaseModel):
    nombre: str
    telefono: str
    pais: Optional[str] = None
    ciudad: Optional[str] = None
    rubro: Optional[str] = None
    etapa: Optional[str] = None            # recién va a emprender | ya tiene negocio (y cuánto)
    modelo_interes: Optional[str] = None
    interes: Optional[str] = None          # comprar | cotizar | taller | visita | informacion
    resumen: Optional[str] = None
    email: Optional[str] = None
    conversation_id: Optional[str] = None


SIN_ODOO = {"ok": False, "mensaje_cliente": "No pude dejar tus datos en este momento. Escríbenos por WhatsApp al "
            "nueve dos cuatro, seis seis dos, dos cero cinco y una asesora te atiende."}


@router.post("/webhook/registrar-interesado")
async def webhook_registrar_interesado(req: InteresadoReq,
                                       x_cevi_secret: Optional[str] = Header(None, alias="X-CeVi-Secret")):
    if not _autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tel = normalizar_telefono(req.telefono, req.pais)
    if not tel:
        return {"ok": False, "falta": "telefono",
                "mensaje_cliente": "Ese número no me quedó claro. ¿Me lo dictas de nuevo, con el código de tu país?"}
    if not (req.nombre or "").strip():
        return {"ok": False, "falta": "nombre", "mensaje_cliente": "¿Me dices tu nombre para que la asesora te ubique?"}
    ahora = time.time()
    for k in [k for k, (t, _) in _RECIENTES.items() if ahora - t > 600]:
        _RECIENTES.pop(k, None)
    if tel in _RECIENTES:
        return {"ok": True, "lead": _RECIENTES[tel][1], "ya_registrado": True,
                "mensaje_cliente": "Tus datos ya quedaron registrados; una asesora de C4V te contacta por WhatsApp."}
    if not odoo_client.enabled():
        return SIN_ODOO
    try:
        r = await run_in_threadpool(
            odoo_client.crear_lead_voz,
            nombre=req.nombre.strip(), telefono=tel, pais=_pais_codigo(req.pais), ciudad=req.ciudad,
            rubro=req.rubro, etapa=req.etapa, modelo_interes=req.modelo_interes, interes=req.interes,
            resumen=req.resumen, email=req.email, conversation_id=req.conversation_id)
    except Exception:
        log.exception("registrar_interesado: no se pudo crear el lead")
        return SIN_ODOO
    _RECIENTES[tel] = (ahora, r["lead_id"])
    if req.conversation_id:
        try:
            await run_in_threadpool(conversaciones.marcar_lead, req.conversation_id, r["lead_id"])
        except Exception:
            log.exception("registrar_interesado: no se pudo anotar el lead en la conversación")
    return {"ok": True, "lead": r["lead_id"], "ya_registrado": r.get("existia", False),
            "mensaje_cliente": "Listo, tus datos quedaron registrados. Una asesora de C4V te contacta por "
                               "WhatsApp para darte el precio exacto y los siguientes pasos."}
