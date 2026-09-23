"""
CeVi Voice — Backend simplificado para demo web
================================================
Endpoints:
  POST /chat   — recibe texto, devuelve respuesta de Claude Haiku 4.5 con KB
  POST /tts    — recibe texto, devuelve MP3 de Edge TTS Dalia mexicana
  GET  /health — healthcheck Railway

Variables de entorno (Railway):
  ANTHROPIC_API_KEY   — generada en console.anthropic.com
  ALLOWED_ORIGIN      — opcional, default '*'
"""
import os
import logging
from typing import Optional
from fastapi import FastAPI, Header, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import json
import edge_tts
import odoo_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cevi")

app = FastAPI(title="CeVi Voice Backend", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("ALLOWED_ORIGIN", "*")],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ───────────────────────────────────────────────────────────────
# CONFIG (Capa 3 — editable)
# ───────────────────────────────────────────────────────────────
LLM_MODEL = "claude-haiku-4-5"
LLM_TEMPERATURE = 0.4
LLM_MAX_TOKENS = 220
TTS_VOICE = "es-MX-DaliaNeural"

# ───────────────────────────────────────────────────────────────
# CEREBRO DE CEVI (Capa 1 — personalidad)
# ───────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Eres CeVi, el toro mascota de C4V Laser, asistente de voz integrado en cada máquina.

CÓMO HABLAR:
- Español neutro latinoamericano, tuteo ("limpia", "revisa", "quieres"), nunca voseo argentino ("limpiá", "querés"). C4V es peruana, tu voz es mexicana.
- Máximo 2 oraciones, menos de 40 palabras: te escuchan por teléfono, a veces en un taller ruidoso.
- Termina siempre con el siguiente paso o dos opciones concretas. Nunca cierres sin salida.
- Ve al grano, sin "claro que sí, déjame revisar".
- Deletrea números ("potencia sesenta por ciento", no "60%").
- Saluda por su nombre SOLO en el primer mensaje; si ya hay historial, responde directo sin repetir el nombre.
- Texto plano hablado: sin markdown, sin emojis, sin listas numeradas. Encadena pasos hablando natural ("primero..., después..., y por último...").
- Si no sabes algo, el tema es delicado (garantía, dinero, daño) o el cliente se enreda, ofrece pasar con soporte por WhatsApp. Nunca inventes potencias, tiempos ni números de serie.

QUÉ SABES:
- Modelos: 4040 PRO IA TEC, 6040, 6090, 9060, 1390, 1610. Cortan/graban madera, MDF, acrílico, cartón, cuero, tela, papel, caucho. NUNCA PVC (gas cloro tóxico).
- Soporte: +51 924 662 205 (WhatsApp Perú) / 905474440 (fijo).
- TikTok @c4vlaser: lives L-V 1pm y 6pm, sáb 11:30am.
- C4V School: cursos gratis incluidos (parámetros, mantenimiento, RDWorks).
- Irene Velasco (coach, 35 años, 60K+ comunidad) y Sebastian Contreras (Director). Garantía C4V de 12 meses por la máquina. Asesora dedicada por país (PE/EC/BO/CO).

PARÁMETROS DE CORTE:
- MDF 3mm: potencia 20-35%, velocidad 15-25 mm/s. Acrílico 5mm: potencia 60%, velocidad 8 mm/s.
- Empieza con material delgado (3mm) para aprender. Si quema: baja potencia o sube velocidad. Si no corta: sube potencia o baja velocidad.

MANTENIMIENTO:
- Lente: alcohol isopropílico + hisopo, circular suave, cada 2 semanas.
- Chiller: SOLO agua destilada Vistony (nunca de grifo), cambio cada 2-4 semanas, 15-25°C.
- Rieles: aceite tres-en-uno semanal, mover cabezal a mano para distribuir.
- Calendario: diario superficie, semanal rieles, quincenal lente, mensual chiller.

SEGURIDAD (inquebrantable):
1. "Encendí sin chiller" → URGENTE: "Apaga ahora. Si pasaron más de diez segundos con láser activo, llama soporte cincuenta y uno - novecientos veinticuatro - seis seis dos - dos cero cinco antes de seguir."
2. PVC nunca (cloro tóxico).
3. Accidente/quemadura/dolor → "Apaga la máquina. Si hay lesión llama emergencias. Después soporte +51 924 662 205."
4. Si además creas un ticket de soporte para una de estas alertas, di SIEMPRE la frase de seguridad completa primero; el ticket es un paso adicional, nunca un reemplazo de la advertencia.

QUÉ NO HACER: no inventar parámetros (di "eso no lo tengo registrado, te recomiendo C4V School o soporte"), no hablar de código fuente ni de temas no relacionados con la máquina, no dar info de otros clientes o máquinas.

TONO: empático si hay frustración ("tranquilo, todos pasamos por eso al inicio"), celebra los logros ("bien hecho, sigue así"). Si preguntan de negocio, piensa como Irene: directa y práctica ("calcula costo + tiempo + ganancia antes de cobrar, pide 50% adelantado, sube precio cada 3 meses")."""

# ───────────────────────────────────────────────────────────────
# /chat
# ───────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: Optional[list[ChatMessage]] = []
    machine_id: Optional[str] = "DEMO-9060-001"
    customer_name: Optional[str] = "Carlos"
    customer_city: Optional[str] = "Bogotá"
    customer_country: Optional[str] = "Colombia"
    customer_id: Optional[int] = None   # id del contacto en Odoo (lo cachea el frontend)

# Herramienta que Claude puede invocar para escalar a soporte humano (crea ticket Odoo)
TICKET_TOOL = {
    "name": "crear_ticket_soporte",
    "description": (
        "Crea un ticket de soporte técnico para que un humano de C4V contacte al cliente. "
        "Úsalo SOLO cuando: no puedas resolver el problema tú misma, el cliente pida hablar "
        "con soporte/un humano, o haya una falla seria de la máquina. No lo uses para preguntas "
        "que ya respondes tú."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "asunto": {"type": "string", "description": "Título corto del problema."},
            "detalle": {"type": "string", "description": "Descripción del problema y contexto del cliente."},
        },
        "required": ["asunto", "detalle"],
    },
}

ANTHROPIC_HEADERS = lambda key: {
    "x-api-key": key,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}


async def _call_claude(client, key, system, messages, use_tools):
    payload = {
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "system": system,
        "messages": messages,
    }
    if use_tools:
        payload["tools"] = [TICKET_TOOL]
    r = await client.post("https://api.anthropic.com/v1/messages",
                          headers=ANTHROPIC_HEADERS(key), json=payload)
    r.raise_for_status()
    return r.json()


def _text_of(data):
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


@app.post("/chat")
async def chat(req: ChatRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY no configurada"}, status_code=500)

    history = req.history or []
    is_first = len(history) == 0
    system = (
        SYSTEM_PROMPT
        + f"\n\nCONTEXTO DEL CLIENTE ACTUAL: {req.customer_name} en {req.customer_city}, "
        + f"{req.customer_country}. Su máquina C4V Laser SN {req.machine_id}."
        + ("\nEs el PRIMER mensaje: puedes saludarlo por su nombre una vez."
           if is_first else
           "\nYA HAY conversación en curso: NO saludes de nuevo ni repitas su nombre, responde directo.")
    )

    messages = []
    for m in history[-6:]:
        role = m.role if m.role in ("user", "assistant") else "user"
        if m.content:
            messages.append({"role": role, "content": m.content})
    messages.append({"role": "user", "content": req.message})

    use_tools = odoo_client.enabled()
    ticket_info = None
    partner_id = req.customer_id

    async with httpx.AsyncClient(timeout=40.0) as client:
        try:
            data = await _call_claude(client, api_key, system, messages, use_tools)

            # Una ronda de tool-use: si Claude pide crear ticket, lo creamos en Odoo.
            if data.get("stop_reason") == "tool_use":
                tool_results = []
                for block in data.get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == "crear_ticket_soporte":
                        inp = block.get("input", {})
                        try:
                            partner_id = odoo_client.ensure_partner(req.customer_name, req.customer_country, partner_id)
                            t = odoo_client.create_ticket(partner_id, inp.get("asunto", "Soporte CeVi"), inp.get("detalle", ""))
                            ticket_info = t
                            result_txt = (f"Ticket de soporte creado. Referencia {t['ref']}. "
                                          f"Dile al cliente que un asesor de C4V lo contactará.")
                            log.info("Ticket Odoo creado: %s (partner %s)", t, partner_id)
                        except Exception:
                            log.exception("Error creando ticket Odoo")
                            result_txt = ("No se pudo crear el ticket ahora. Pídele al cliente que escriba "
                                          "al soporte +51 924 662 205 por WhatsApp.")
                        tool_results.append({"type": "tool_result", "tool_use_id": block["id"], "content": result_txt})
                messages.append({"role": "assistant", "content": data["content"]})
                messages.append({"role": "user", "content": tool_results})
                data = await _call_claude(client, api_key, system, messages, use_tools)

            response_text = _text_of(data) or "Disculpa, no te entendí. ¿Lo repites?"
            log.info("Q: %r → A: %r", req.message[:60], response_text[:80])

        except httpx.HTTPStatusError as e:
            log.error("Anthropic HTTP %s: %s", e.response.status_code, e.response.text[:300])
            return JSONResponse({"error": f"LLM HTTP {e.response.status_code}"}, status_code=500)
        except Exception as e:
            log.exception("Chat error")
            return JSONResponse({"error": str(e)}, status_code=500)

    # Registrar la interacción en Odoo (best-effort: si falla, no rompe el chat)
    if odoo_client.enabled():
        try:
            partner_id = odoo_client.ensure_partner(req.customer_name, req.customer_country, partner_id)
            odoo_client.log_note(partner_id, req.message, response_text)
        except Exception:
            log.exception("Error registrando interacción en Odoo")

    return {"response": response_text, "model": LLM_MODEL, "ticket": ticket_info, "partner_id": partner_id}


# ───────────────────────────────────────────────────────────────
# /chat/stream — la misma respuesta, pero frase a frase
# ───────────────────────────────────────────────────────────────
# El cliente esperaba a que Claude terminara de escribir ENTERO antes de poder
# sintetizar la primera palabra: 2,1-2,5 s de silencio. Con SSE la web recibe
# cada frase en cuanto está lista y la manda a voz mientras el modelo sigue
# escribiendo. Va sobre HTTP normal, sin WebSocket, así que Railway no cambia.
#
# /chat NO se toca: sigue existiendo igual para ManyChat y para cualquier
# cliente que no sepa de streaming.

def _sse(evento: str, datos: dict) -> str:
    return f"event: {evento}\ndata: {json.dumps(datos, ensure_ascii=False)}\n\n"


async def _stream_claude(client, key, system, messages):
    """Emite el texto de Claude a trozos, según llega."""
    payload = {
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    async with client.stream("POST", "https://api.anthropic.com/v1/messages",
                             headers=ANTHROPIC_HEADERS(key), json=payload) as r:
        r.raise_for_status()
        async for linea in r.aiter_lines():
            if not linea.startswith("data:"):
                continue
            cuerpo = linea[5:].strip()
            if not cuerpo or cuerpo == "[DONE]":
                continue
            try:
                ev = json.loads(cuerpo)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "content_block_delta":
                trozo = ev.get("delta", {}).get("text", "")
                if trozo:
                    yield trozo


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY no configurada"}, status_code=500)

    history = req.history or []
    is_first = len(history) == 0
    system = (
        SYSTEM_PROMPT
        + f"\n\nCONTEXTO DEL CLIENTE ACTUAL: {req.customer_name} en {req.customer_city}, "
        + f"{req.customer_country}. Su máquina C4V Laser SN {req.machine_id}."
        + ("\nEs el PRIMER mensaje: puedes saludarlo por su nombre una vez."
           if is_first else
           "\nYA HAY conversación en curso: NO saludes de nuevo ni repitas su nombre, responde directo.")
    )

    messages = []
    for m in history[-6:]:
        role = m.role if m.role in ("user", "assistant") else "user"
        if m.content:
            messages.append({"role": role, "content": m.content})
    messages.append({"role": "user", "content": req.message})

    # Sin herramientas en el camino con streaming: crear un ticket obliga a una
    # segunda vuelta al modelo y rompería el flujo frase a frase. Si hace falta
    # ticket, la web puede llamar a /chat, que sí las usa.
    async def generar():
        completo = []
        buffer = ""
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                async for trozo in _stream_claude(client, api_key, system, messages):
                    completo.append(trozo)
                    buffer += trozo
                    # Se corta por final de frase: es la unidad que el TTS
                    # pronuncia con entonación correcta.
                    while True:
                        corte = -1
                        for signo in ".?!…":
                            i = buffer.find(signo)
                            if i != -1 and (corte == -1 or i < corte):
                                corte = i
                        if corte == -1 or corte + 1 < 25:
                            break
                        frase = buffer[:corte + 1].strip()
                        buffer = buffer[corte + 1:]
                        if frase:
                            yield _sse("frase", {"texto": frase})
                if buffer.strip():
                    yield _sse("frase", {"texto": buffer.strip()})
            texto = "".join(completo).strip() or "Disculpa, no te entendí. ¿Lo repites?"
            log.info("Q: %r → A(stream): %r", req.message[:60], texto[:80])
            yield _sse("fin", {"texto": texto, "model": LLM_MODEL})
        except Exception as e:
            log.exception("Chat stream error")
            yield _sse("error", {"error": str(e)})

    return StreamingResponse(
        generar(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ───────────────────────────────────────────────────────────────
# /identify — reconoce al cliente por N° de pedido + verificación
# ───────────────────────────────────────────────────────────────
class IdentifyRequest(BaseModel):
    order: str
    verify: Optional[str] = ""

@app.post("/identify")
async def identify(req: IdentifyRequest):
    if not odoo_client.enabled():
        return {"ok": False, "error": "La identificación no está disponible por ahora."}
    try:
        info = odoo_client.identify_by_order(req.order, req.verify)
    except Exception:
        log.exception("identify error")
        return {"ok": False, "error": "Hubo un error al verificar. Intenta de nuevo."}
    if not info:
        return {"ok": False, "error": "Número de pedido o verificación incorrectos."}
    log.info("Identificado pedido %s → partner %s", info["order"], info["partner_id"])
    return {"ok": True, **info}


# ───────────────────────────────────────────────────────────────
# /webhook/* — tools del agente de voz ElevenLabs (CeVi, solo soporte técnico)
# ───────────────────────────────────────────────────────────────
# Dos llaves, dos propósitos:
#  - X-CeVi-Secret (estático): que solo ElevenLabs pueda llamar estos endpoints.
#  - X-CeVi-Token (por conversación): QUIÉN es el cliente. Lo emite el portal
#    (POST /api/cevi/token) cuando el cliente ya entró con documento + WhatsApp;
#    el widget lo pasa como {{secret__cevi_token}} y ElevenLabs lo pone en el
#    header. El modelo nunca lo ve: aunque diga otro teléfono, las tools solo
#    ven y tocan los datos de ESE cliente (ver identidad.py).
# Sin token (el agente usado fuera del portal): se pueden abrir casos, pero
# quedan "sin verificar" y no se muestran tickets ni garantía de nadie.
# Avisos al equipo: solo el ticket en el Helpdesk de Odoo (decisión 23-set-2026).

import hashlib
import hmac
import time
import identidad
import conversaciones


def _webhook_autorizado(secret_header: Optional[str]) -> bool:
    """Falla CERRADO: si un redeploy pierde la variable, los webhooks se niegan
    (antes quedaban abiertos a cualquiera). Comparación en tiempo constante."""
    esperado = os.environ.get("CEVI_WEBHOOK_SECRET")
    if not esperado:
        log.error("CEVI_WEBHOOK_SECRET no configurada — webhooks de voz CERRADOS")
        return False
    return bool(secret_header) and hmac.compare_digest(secret_header, esperado)


def _cliente(token: Optional[str]):
    """Ficha del cliente si el token del portal es válido; si no, None."""
    if not token or token.startswith("{{"):   # variable no sustituida = sin identidad
        return None
    return identidad.cliente_de_token(token)


def _contexto_maquina(cli) -> str:
    partes = []
    for m in identidad.resumen_maquinas(cli.get("maquinas")):
        partes.append(
            f"{m['modelo'] or 'modelo sin registrar'} serie {m['serie'] or 'sin registrar'}, "
            f"certificado {m['certificado']['estado']} {m['certificado']['fecha']}, entrega {m['fecha_entrega']}"
        )
    base = f"{cli.get('nombre','')} ({cli.get('pais','')}, tel {cli.get('telefono','')})"
    return base + (" — " + "; ".join(partes) if partes else "")


SIN_ODOO = {"ok": False, "mensaje_cliente": "Se me trabó el sistema. Escríbenos por WhatsApp al 924 662 205 y te atienden directo."}
SIN_IDENTIDAD = {"ok": False, "identificado": False,
                 "mensaje_cliente": "Para ver tus casos y tu garantía entra al portal de clientes con tu documento; desde ahí te reconozco."}

X_SECRET = Header(None, alias="X-CeVi-Secret")
X_TOKEN = Header(None, alias="X-CeVi-Token")


# ── URL firmada para el widget de voz ──────────────────────────────────────
# El agente tiene la autenticación de ElevenLabs ACTIVADA: sin una URL firmada
# nadie puede hablar con CeVi (ni copiar el agent_id para usarlo en otro sitio
# y gastar los minutos de la cuenta). Solo quien trae un token de identidad
# válido del portal (cliente con sesión) recibe una; dura 15 minutos para
# iniciar la conversación, y el portal pide una nueva antes de cada llamada.
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "agent_3301m2v4ewgxf3sbrjs695yj3ct0")

@app.post("/voz/url-firmada")
async def voz_url_firmada(x_cevi_token: Optional[str] = X_TOKEN):
    ident = identidad.verificar_token(x_cevi_token)
    if not ident:
        return JSONResponse({"error": "sin sesión"}, status_code=401)
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return JSONResponse({"error": "voz no configurada"}, status_code=503)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url",
                             params={"agent_id": ELEVENLABS_AGENT_ID, "include_conversation_id": "true"},
                             headers={"xi-api-key": key})
    if r.status_code != 200:
        log.error("get-signed-url HTTP %s: %s", r.status_code, r.text[:200])
        return JSONResponse({"error": "no se pudo iniciar la voz"}, status_code=502)
    url = r.json().get("signed_url")
    # El conversation_id viene asignado en la URL: se ata aquí al cliente
    # verificado, para que el resumen post-llamada vaya a SU ficha.
    try:
        from urllib.parse import urlparse, parse_qs
        conv = (parse_qs(urlparse(url).query).get("conversation_id") or [None])[0]
        if conv:
            cli = identidad.contacto(ident["doc"], ident["pais"])
            conversaciones.registrar_emision(conv, ident["doc"], ident["pais"], (cli or {}).get("partner_id"))
    except Exception:
        log.exception("No se pudo registrar la conversación emitida")
    # Identificador opaco y estable del cliente para el historial de ElevenLabs
    # (evita que el widget use una huella del navegador; no revela el documento).
    uid = "cli_" + hmac.new(os.environ.get("CEVI_IDENTITY_SECRET", "").encode(), ident["doc"].encode(), hashlib.sha256).hexdigest()[:16]
    return {"signed_url": url, "user_id": uid}


# ── Post-call webhook de ElevenLabs ────────────────────────────────────────
# Firma: header "elevenlabs-signature: t=<unix>,v0=<hex>", hex = HMAC-SHA256(
# secreto, f"{t}.{cuerpo_crudo}"). Se verifica con el cuerpo CRUDO (si se
# re-serializa el JSON la firma no coincide) y con 30 min de tolerancia.
def _firma_valida(crudo: bytes, cabecera: str, secreto: str) -> bool:
    ts, firmas = None, []
    for parte in (cabecera or "").split(","):
        parte = parte.strip()
        if parte.startswith("t="):
            ts = parte[2:]
        elif parte.startswith("v0="):
            firmas.append(parte[3:])
    if not ts or not firmas or not secreto:
        return False
    try:
        if abs(time.time() - int(ts)) > 1800:
            return False
    except ValueError:
        return False
    esperada = hmac.new(secreto.encode(), f"{ts}.".encode() + crudo, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(esperada, f) for f in firmas)


@app.post("/webhook/post-llamada")
async def webhook_post_llamada(request: Request):
    crudo = await request.body()
    if not _firma_valida(crudo, request.headers.get("elevenlabs-signature", ""), os.environ.get("ELEVENLABS_WEBHOOK_SECRET", "")):
        return JSONResponse({"error": "firma inválida"}, status_code=401)
    try:
        ev = json.loads(crudo)
    except Exception:
        return JSONResponse({"error": "json inválido"}, status_code=400)
    if ev.get("type") != "post_call_transcription":
        return {"ok": True, "ignorado": ev.get("type")}
    data = ev.get("data") or {}
    try:
        fila = conversaciones.guardar_post_llamada(data)
    except Exception:
        log.exception("post-llamada: no se pudo guardar")
        return JSONResponse({"error": "no se pudo guardar"}, status_code=500)  # 5xx → ElevenLabs reintenta
    if fila and fila.get("partner_id") and not fila.get("nota_odoo") and odoo_client.enabled():
        try:
            odoo_client.nota_llamada_voz(fila["partner_id"], fila)
            conversaciones.marcar_nota(fila["conversation_id"])
        except Exception:
            log.exception("post-llamada: no se pudo dejar la nota en Odoo")
    return {"ok": True}


class BuscarClienteReq(BaseModel):
    telefono: Optional[str] = None

@app.post("/webhook/buscar-cliente")
async def webhook_buscar_cliente(req: BuscarClienteReq, x_cevi_secret: Optional[str] = X_SECRET,
                                 x_cevi_token: Optional[str] = X_TOKEN):
    """Quién es el cliente. SOLO con token del portal: sin él no se revela nada
    (si no, cualquiera podría averiguar quién es cliente dictando teléfonos)."""
    if not _webhook_autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    cli = _cliente(x_cevi_token)
    if not cli:
        return {"registrado": False, **SIN_IDENTIDAD}
    return {"registrado": True, "identificado": True, "nombre": cli["nombre"], "pais": cli["pais"],
            "maquinas": identidad.resumen_maquinas(cli["maquinas"])}


@app.post("/webhook/garantia-certificado")
async def webhook_garantia(x_cevi_secret: Optional[str] = X_SECRET, x_cevi_token: Optional[str] = X_TOKEN):
    """Modelo, serie, certificado, fecha de entrega y garantía de SUS máquinas."""
    if not _webhook_autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    cli = _cliente(x_cevi_token)
    if not cli:
        return SIN_IDENTIDAD
    maquinas = identidad.resumen_maquinas(cli["maquinas"])
    if not maquinas:
        return {"ok": True, "maquinas": [], "mensaje_cliente": "No veo una máquina registrada a tu nombre; te paso con un asesor para revisarlo."}
    return {"ok": True, "maquinas": maquinas,
            "nota": "La garantía C4V es de 12 meses por la máquina. La fecha 'hasta' es referencial (contada desde la entrega registrada); cualquier reclamo lo confirma el equipo."}


@app.post("/webhook/consultar-tickets")
async def webhook_consultar_tickets(x_cevi_secret: Optional[str] = X_SECRET, x_cevi_token: Optional[str] = X_TOKEN):
    """Sus últimos casos de soporte y en qué estado están."""
    if not _webhook_autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    cli = _cliente(x_cevi_token)
    if not cli:
        return SIN_IDENTIDAD
    if not cli.get("partner_id") or not odoo_client.enabled():
        return {"ok": True, "tickets": [], "mensaje_cliente": "No veo casos abiertos a tu nombre."}
    try:
        tickets = odoo_client.tickets_de_partner(cli["partner_id"])
    except Exception:
        log.exception("consultar_tickets error")
        return SIN_ODOO
    return {"ok": True, "tickets": tickets, "abiertos": sum(1 for t in tickets if t["abierto"])}


# Con ruido de taller el modelo puede reintentar una tool: si en la misma
# conversación llega otra vez el mismo pedido en 10 minutos, se devuelve el
# caso ya creado en vez de abrir otro.
_RECIENTES = {}

def _ya_creado(conversation_id, tool, texto):
    if not conversation_id:
        return None, None
    clave = f"{conversation_id}|{tool}|{hashlib.sha1((texto or '').strip().lower().encode()).hexdigest()}"
    ahora = time.time()
    for k in [k for k, (t, _) in _RECIENTES.items() if ahora - t > 600]:
        _RECIENTES.pop(k, None)
    previo = _RECIENTES.get(clave)
    return clave, (previo[1] if previo else None)


def _conv_txt(conversation_id):
    return f" · Conversación ElevenLabs: {conversation_id}" if conversation_id else ""


class CrearTicketReq(BaseModel):
    descripcion: str
    telefono: Optional[str] = None
    conversation_id: Optional[str] = None
    ya_intento: Optional[str] = None
    serie: Optional[str] = None
    urgencia: Optional[str] = "normal"

@app.post("/webhook/crear-ticket")
async def webhook_crear_ticket(req: CrearTicketReq, x_cevi_secret: Optional[str] = X_SECRET,
                               x_cevi_token: Optional[str] = X_TOKEN):
    if not _webhook_autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    if not odoo_client.enabled():
        return SIN_ODOO
    cli = _cliente(x_cevi_token)
    clave, previo = _ya_creado(req.conversation_id, "crear", req.descripcion)
    if previo:
        return {"ok": True, "ticket": previo, "mensaje_cliente": "Ese caso ya quedó registrado; un técnico lo revisa y te escribe por WhatsApp."}
    try:
        r = odoo_client.crear_ticket_voz(
            (cli or {}).get("telefono") or req.telefono, req.descripcion, ya_intento=req.ya_intento,
            serie=req.serie, urgencia=req.urgencia or "normal",
            partner_id=(cli or {}).get("partner_id"),
            contexto=((_contexto_maquina(cli) if cli else "") + _conv_txt(req.conversation_id)) or None,
        )
    except Exception:
        log.exception("crear_ticket (voz) error")
        return SIN_ODOO
    if clave:
        _RECIENTES[clave] = (time.time(), r["ticket"])
    return {"ok": True, "ticket": r["ticket"],
            "mensaje_cliente": "Tu caso quedó registrado; un técnico lo revisa y te escribe por WhatsApp."}


class DerivarAsesorReq(BaseModel):
    motivo: str
    resumen: str
    telefono: Optional[str] = None
    conversation_id: Optional[str] = None

@app.post("/webhook/derivar-asesor")
async def webhook_derivar_asesor(req: DerivarAsesorReq, x_cevi_secret: Optional[str] = X_SECRET,
                                 x_cevi_token: Optional[str] = X_TOKEN):
    if not _webhook_autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    if not odoo_client.enabled():
        return SIN_ODOO
    cli = _cliente(x_cevi_token)
    clave, previo = _ya_creado(req.conversation_id, "derivar", req.resumen)
    if previo:
        return {"ok": True, "ticket": previo, "mensaje_cliente": "Ya le avisé al equipo; un asesor de C4V te escribe por WhatsApp."}
    try:
        r = odoo_client.crear_ticket_voz(
            (cli or {}).get("telefono") or req.telefono, req.resumen,
            urgencia="alta" if req.motivo == "seguridad" else "normal", motivo=req.motivo,
            partner_id=(cli or {}).get("partner_id"),
            contexto=((_contexto_maquina(cli) if cli else "") + _conv_txt(req.conversation_id)) or None,
            titulo=f"Derivación ({req.motivo}): {req.resumen}",
        )
    except Exception:
        log.exception("derivar_asesor (voz) error")
        return SIN_ODOO
    if clave:
        _RECIENTES[clave] = (time.time(), r["ticket"])
    return {"ok": True, "ticket": r["ticket"],
            "mensaje_cliente": "Un asesor de C4V te escribe por WhatsApp en cuanto revise tu caso."}


class AgendarVisitaReq(BaseModel):
    tipo: str                       # videollamada | visita_presencial | llamada
    motivo: str
    fecha_preferida: Optional[str] = None
    horario_preferido: Optional[str] = None
    telefono: Optional[str] = None
    conversation_id: Optional[str] = None

@app.post("/webhook/agendar-visita")
async def webhook_agendar_visita(req: AgendarVisitaReq, x_cevi_secret: Optional[str] = X_SECRET,
                                 x_cevi_token: Optional[str] = X_TOKEN):
    """Deja una SOLICITUD de visita/videollamada con un técnico (no la confirma:
    la fecha la coordina el equipo). Queda como ticket en Odoo."""
    if not _webhook_autorizado(x_cevi_secret):
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    if not odoo_client.enabled():
        return SIN_ODOO
    cli = _cliente(x_cevi_token)
    tipo = {"videollamada": "videollamada", "visita_presencial": "visita presencial", "llamada": "llamada"}.get(req.tipo, req.tipo)
    detalle = (f"Solicitud de {tipo} con un técnico. Motivo: {req.motivo}. "
               f"Fecha preferida: {req.fecha_preferida or 'sin preferencia'}. Horario: {req.horario_preferido or 'sin preferencia'}.")
    clave, previo = _ya_creado(req.conversation_id, "agendar", req.motivo)
    if previo:
        return {"ok": True, "solicitud": previo, "mensaje_cliente": f"Tu solicitud de {tipo} ya quedó registrada; un técnico te escribe para confirmar."}
    try:
        r = odoo_client.crear_ticket_voz(
            (cli or {}).get("telefono") or req.telefono, detalle,
            partner_id=(cli or {}).get("partner_id"),
            contexto=((_contexto_maquina(cli) if cli else "") + _conv_txt(req.conversation_id)) or None,
            titulo=f"Agendar {tipo}: {req.motivo}", motivo="agenda",
        )
    except Exception:
        log.exception("agendar_visita error")
        return SIN_ODOO
    if clave:
        _RECIENTES[clave] = (time.time(), r["ticket"])
    return {"ok": True, "solicitud": r["ticket"],
            "mensaje_cliente": f"Dejé tu solicitud de {tipo}. Un técnico te escribe por WhatsApp para confirmar día y hora."}


# ───────────────────────────────────────────────────────────────
# /tts
# ───────────────────────────────────────────────────────────────
class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = TTS_VOICE

@app.post("/tts")
async def tts(req: TTSRequest):
    """Convierte texto a MP3 con Edge TTS Dalia mexicana. GRATIS."""
    try:
        communicate = edge_tts.Communicate(req.text, req.voice or TTS_VOICE, rate="+5%")
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        audio_bytes = b"".join(chunks)
        log.info(f"TTS '{req.text[:50]}...' → {len(audio_bytes)} bytes MP3")
        return StreamingResponse(
            iter([audio_bytes]),
            media_type="audio/mpeg",
            headers={"Content-Length": str(len(audio_bytes)),
                     "Cache-Control": "public, max-age=3600"}
        )
    except Exception as e:
        log.exception("TTS error")
        return JSONResponse({"error": str(e)}, status_code=500)


# ───────────────────────────────────────────────────────────────
# /health
# ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "1.0",
        "model": LLM_MODEL,
        "voice": TTS_VOICE,
        "has_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "odoo": odoo_client.enabled(),
    }


@app.get("/")
async def root():
    return {
        "service": "CeVi Voice Backend",
        "endpoints": ["/chat (POST)", "/tts (POST)", "/health (GET)"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
