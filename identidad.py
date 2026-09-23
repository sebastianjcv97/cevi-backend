"""
Identidad del cliente para CeVi (agente de voz de ElevenLabs)
==============================================================
El cliente ya entró al portal (documento + código por WhatsApp). El portal
(portal-api, POST /api/cevi/token) le da al widget un token corto firmado con
CEVI_IDENTITY_SECRET; ElevenLabs lo manda en el header X-CeVi-Token de cada
tool como variable secreta — el modelo nunca lo ve ni lo puede cambiar.

Aquí solo se VERIFICA ese token (no se emiten: eso es del portal) y se lee la
ficha del cliente en c4v.portal_contacts, la misma tabla contra la que el
portal lo dejó entrar. Así las tools de CeVi saben quién es y solo pueden ver
SUS datos, aunque el modelo diga otro teléfono.

Formato del token (idéntico a portal/src/ceviToken.js):
  base64url(json) + '.' + base64url(HMAC-SHA256)   json = {s:'cevi', d, p, exp}

Variables de entorno: CEVI_IDENTITY_SECRET, POSTGRES_URL.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import date, datetime

log = logging.getLogger("cevi.identidad")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def verificar_token(token):
    """Devuelve {'doc', 'pais'} si el token es válido y vigente; si no, None."""
    secreto = os.environ.get("CEVI_IDENTITY_SECRET") or ""
    t = str(token or "")
    if len(secreto) < 16 or "." not in t:
        return None
    cuerpo, firma = t.split(".", 1)
    esperada = _b64u(hmac.new(secreto.encode(), cuerpo.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(firma, esperada):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cuerpo + "=" * (-len(cuerpo) % 4)))
    except Exception:
        return None
    if payload.get("s") != "cevi" or not payload.get("d") or payload.get("exp", 0) < time.time() * 1000:
        return None
    return {"doc": str(payload["d"]), "pais": str(payload.get("p") or "")}


def contacto(doc, pais=""):
    """Ficha del cliente en c4v.portal_contacts (la del portal). None si no hay."""
    url = os.environ.get("POSTGRES_URL")
    if not url:
        return None
    import psycopg  # import tardío: si falta la librería, el resto del backend sigue
    with psycopg.connect(url, connect_timeout=5) as conn:
        fila = conn.execute(
            """select odoo_partner_id, nombre, pais, ciudad, telefono, maquinas, empresa_vendedora
                 from c4v.portal_contacts
                where documento_norm = %s
                order by (pais = %s) desc
                limit 1""",
            (doc, (pais or "").upper()),
        ).fetchone()
        # Clientes sin DNI/RUC en Odoo: el portal los identifica como "P<id de
        # Odoo>" (login por teléfono, 23-set-2026). Si no hay ficha con ese
        # documento, se busca por el id de Odoo.
        if not fila and doc[:1].upper() == "P" and doc[1:].isdigit():
            fila = conn.execute(
                """select odoo_partner_id, nombre, pais, ciudad, telefono, maquinas, empresa_vendedora
                     from c4v.portal_contacts where odoo_partner_id = %s limit 1""",
                (int(doc[1:]),),
            ).fetchone()
    if not fila:
        return None
    pid, nombre, pais_, ciudad, tel, maquinas, empresa = fila
    if isinstance(maquinas, str):
        try:
            maquinas = json.loads(maquinas)
        except Exception:
            maquinas = []
    return {
        "partner_id": pid if pid and pid > 0 else None,  # <= 0: ficha manual sin Odoo
        "nombre": nombre or "",
        "pais": pais_ or "",
        "ciudad": ciudad or "",
        "telefono": (tel or "").split(",")[0].strip(),
        "maquinas": maquinas or [],
        "empresa_vendedora": empresa or "",
    }


def cliente_de_token(token):
    """Atajo: token → ficha. None si el token no vale o no hay ficha."""
    ident = verificar_token(token)
    if not ident:
        return None
    try:
        return contacto(ident["doc"], ident["pais"])
    except Exception:
        log.exception("No se pudo leer la ficha del portal")
        return None


def _fecha(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _mas_meses(d, meses):
    y, m = divmod(d.month - 1 + meses, 12)
    y, m = d.year + y, m + 1
    for dia in (d.day, 30, 29, 28):
        try:
            return date(y, m, dia)
        except ValueError:
            continue


GRANDES = ("1390", "1610", "13100", "16100", "18120", "1812")  # instalación presencial


def resumen_maquinas(maquinas, hoy=None):
    """Lo que el cliente puede saber de su máquina: modelo, serie, certificado,
    entrega y garantía. La garantía C4V es de 12 meses por la máquina; se cuenta
    desde la entrega REGISTRADA y se dice como referencial (la confirma el equipo)."""
    hoy = hoy or date.today()
    out = []
    for m in maquinas or []:
        cert = m.get("certificado") or {}
        entrega = _fecha(m.get("fecha_entrega"))
        garantia = None
        if entrega:
            hasta = _mas_meses(entrega, 12)
            garantia = {
                "meses": 12,
                "desde_entrega": entrega.isoformat(),
                "hasta_referencial": hasta.isoformat(),
                "vigente": hoy <= hasta,
                "dias_restantes": max((hasta - hoy).days, 0),
            }
        modelo = str(m.get("modelo") or "")
        out.append({
            "modelo": modelo,
            "serie": m.get("serie") or "",
            "certificado": {
                "estado": cert.get("estado") or "sin certificado registrado",
                "fecha": cert.get("fecha") or "",
            },
            "fecha_entrega": entrega.isoformat() if entrega else "no registrada",
            "garantia": garantia or {"meses": 12, "nota": "No hay fecha de entrega registrada; la vigencia la confirma el equipo."},
            "instalacion": "presencial" if any(g in modelo for g in GRANDES) else "remota (videollamada)",
        })
    return out
