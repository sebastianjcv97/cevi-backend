"""
CeVi ↔ Odoo — cliente XML-RPC (lado servidor, nunca el frontend)
================================================================
Escribe en el Odoo de C4V (erp.c4vlaser.com):
  - busca/crea el contacto del cliente (res.partner), etiquetado "CeVi Demo"
  - registra cada conversación como nota en la ficha del cliente
  - crea tickets de Helpdesk (equipo "CeVi", etiqueta "CeVi Demo")

Variables de entorno (Railway):
  ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD

Si faltan, enabled() es False y el backend simplemente NO toca Odoo
(el chat sigue funcionando igual).
"""
import os
import ssl
import logging
import threading
import xmlrpc.client

log = logging.getLogger("cevi.odoo")

ODOO_URL = (os.environ.get("ODOO_URL") or "").rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USER = os.environ.get("ODOO_USER")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

# Separación de datos demo (decisión 2026-06-09: equipo + etiqueta "CeVi Demo")
TEAM_NAME = "CeVi"
DEMO_TAG = "CeVi Demo"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

_lock = threading.Lock()
_state = {"uid": None, "models": None, "team_id": None, "hd_tag_id": None, "cat_id": None}


def enabled() -> bool:
    return all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD])


def _connect():
    """Autentica y cachea uid/models + asegura equipo y etiquetas demo. Idempotente."""
    if _state["uid"]:
        return
    with _lock:
        if _state["uid"]:
            return
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", context=_ctx, allow_none=True)
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise RuntimeError("Odoo: autenticación fallida")
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", context=_ctx, allow_none=True)
        _state["uid"] = uid
        _state["models"] = models
        _ensure_setup()
        log.info("Odoo conectado (uid=%s, team=%s)", uid, _state["team_id"])


def _ex(model, method, *args, **kw):
    return _state["models"].execute_kw(ODOO_DB, _state["uid"], ODOO_PASSWORD, model, method, list(args), kw)


def _find_or_create(model, domain, vals):
    ids = _ex(model, "search", domain, limit=1)
    if ids:
        return ids[0]
    return _ex(model, "create", vals)


def _ensure_setup():
    _state["team_id"] = _find_or_create("helpdesk.team", [["name", "=", TEAM_NAME]], {"name": TEAM_NAME})
    _state["hd_tag_id"] = _find_or_create("helpdesk.tag", [["name", "=", DEMO_TAG]], {"name": DEMO_TAG})
    _state["cat_id"] = _find_or_create("res.partner.category", [["name", "=", DEMO_TAG]], {"name": DEMO_TAG})


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ensure_partner(name, country=None, partner_id=None):
    """Devuelve el id del contacto. Reusa partner_id si es válido; si no, busca por
    nombre dentro de los etiquetados 'CeVi Demo'; si no existe, lo crea."""
    _connect()
    name = (name or "Cliente").strip() or "Cliente"
    if partner_id:
        try:
            if _ex("res.partner", "search_count", [["id", "=", int(partner_id)]]):
                return int(partner_id)
        except Exception:
            pass
    cat = _state["cat_id"]
    ids = _ex("res.partner", "search", [["name", "=", name], ["category_id", "in", [cat]]], limit=1)
    if ids:
        return ids[0]
    vals = {"name": name, "category_id": [(4, cat)], "comment": "Contacto creado por CeVi (demo web)."}
    if country:
        vals["comment"] += f" País: {country}."
    return _ex("res.partner", "create", vals)


def log_note(partner_id, user_msg, cevi_reply):
    """Registra la interacción como nota interna en la ficha del cliente."""
    _connect()
    body = (
        "<p><b>🎙️ CeVi (asistente web)</b></p>"
        f"<p><b>Cliente:</b> {_esc(user_msg)}</p>"
        f"<p><b>CeVi:</b> {_esc(cevi_reply)}</p>"
    )
    _ex("res.partner", "message_post", [int(partner_id)], body=body, subtype_xmlid="mail.mt_note")


def create_ticket(partner_id, subject, detail):
    """Crea un ticket de Helpdesk en el equipo 'CeVi' con la etiqueta 'CeVi Demo'.
    Devuelve {'id', 'ref'}."""
    _connect()
    vals = {
        "name": (subject or "Soporte solicitado vía CeVi")[:120],
        "partner_id": int(partner_id),
        "description": f"<p>{_esc(detail)}</p><p><i>Generado por CeVi (asistente web).</i></p>",
        "team_id": _state["team_id"],
        "tag_ids": [(4, _state["hd_tag_id"])],
    }
    tid = _ex("helpdesk.ticket", "create", vals)
    ref = None
    try:
        rec = _ex("helpdesk.ticket", "read", [tid], fields=["ticket_ref"])
        if rec:
            ref = rec[0].get("ticket_ref")
    except Exception:
        pass
    return {"id": tid, "ref": ref or str(tid)}
