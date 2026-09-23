"""
CeVi ↔ Odoo — cliente XML-RPC (lado servidor, nunca el frontend)
================================================================
Escribe en el Odoo de C4V (erp.c4vlaser.com):
  - busca/crea el contacto del cliente (res.partner), etiquetado "CeVi Demo"
  - registra cada conversación como nota en la ficha del cliente
  - crea tickets de Helpdesk (equipo "CeVi", etiqueta "CeVi Demo")
  - (18-set-2026) busca clientes REALES por teléfono y crea tickets reales
    etiquetados "CeVi Voz", para las tools del agente de voz ElevenLabs
    (ver buscar_por_telefono / crear_ticket_voz más abajo)

Variables de entorno (Railway):
  ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD

Si faltan, enabled() es False y el backend simplemente NO toca Odoo
(el chat sigue funcionando igual).
"""
import os
import re
import ssl
import time
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
# Datos REALES creados por el agente de voz de ElevenLabs (decisión 18-set-2026):
# etiqueta propia para distinguirlos de los de prueba, mismo equipo de Helpdesk.
VOZ_TAG = "CeVi Voz"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

_lock = threading.Lock()
_state = {
    "uid": None, "models": None, "team_id": None, "hd_tag_id": None, "cat_id": None,
    "voz_tag_id": None,
}


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
    _state["voz_tag_id"] = _find_or_create("helpdesk.tag", [["name", "=", VOZ_TAG]], {"name": VOZ_TAG})


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


# ── Anti fuerza bruta: máx 6 intentos por pedido en 10 min ──
_attempts = {}
def _too_many(order):
    now = time.time()
    arr = [t for t in _attempts.get(order, []) if now - t < 600]
    _attempts[order] = arr
    return len(arr) >= 6
def _note_fail(order):
    _attempts.setdefault(order, []).append(time.time())


def identify_by_order(order_name, verify):
    """Identifica al cliente por N° de pedido (S#####) + verificación (últimos 4
    dígitos de teléfono O DNI/RUC). Devuelve {partner_id,name,order,machine,country}
    o None (genérico, sin revelar qué falló). Anti fuerza bruta por pedido."""
    _connect()
    order_name = (order_name or "").strip().upper()
    if not order_name or _too_many(order_name):
        return None
    oids = _ex("sale.order", "search", [["name", "=", order_name]], limit=1)
    if not oids:
        _note_fail(order_name)
        return None
    o = _ex("sale.order", "read", [oids[0]], fields=["partner_id", "order_line"])[0]
    if not o.get("partner_id"):
        _note_fail(order_name)
        return None
    pid = o["partner_id"][0]
    p = _ex("res.partner", "read", [pid], fields=["name", "phone", "vat", "country_id"])[0]
    last4 = re.sub(r"\D", "", verify or "")[-4:]
    dphone = re.sub(r"\D", "", p.get("phone") or "")
    dvat = re.sub(r"\D", "", p.get("vat") or "")
    if not last4 or (last4 != dphone[-4:] and last4 != dvat[-4:]):
        _note_fail(order_name)
        return None
    machine = None
    if o.get("order_line"):
        for ln in _ex("sale.order.line", "read", o["order_line"], fields=["name"]):
            nm = ln.get("name") or ""
            if any(k in nm.lower() for k in ["laser", "machine", "tec", "pro", "co2", "fibra"]):
                machine = nm.split("]")[-1].strip().split("\n")[0][:60]
                break
    return {
        "partner_id": pid,
        "name": p.get("name") or "Cliente",
        "order": order_name,
        "machine": machine,
        "country": (p["country_id"][1] if p.get("country_id") else ""),
    }


# ── Tools del agente de voz ElevenLabs (18-set-2026) — clientes y tickets REALES ──
# A diferencia de ensure_partner/create_ticket (arriba), que son para la demo web
# ("CeVi Demo"), esto busca en la cartera real de clientes y crea tickets reales,
# etiquetados "CeVi Voz" para que se distingan en Odoo. Sin WhatsApp automático
# todavía: el ticket queda visible en el Helpdesk, pero no avisa solo — hace
# falta que el equipo revise la bandeja (o construir el envío de WhatsApp, A6).

def _extraer_maquinas(order_line_ids):
    """De líneas de sale.order, devuelve nombres de modelo (heurística: palabras
    clave de máquina láser), igual que en identify_by_order."""
    if not order_line_ids:
        return []
    maquinas = []
    for ln in _ex("sale.order.line", "read", order_line_ids, fields=["name"]):
        nm = ln.get("name") or ""
        if any(k in nm.lower() for k in ["laser", "láser", "machine", "tec", "pro", "co2", "fibra"]):
            modelo = nm.split("]")[-1].strip().split("\n")[0][:60]
            if modelo and modelo not in maquinas:
                maquinas.append(modelo)
    return maquinas


def buscar_por_telefono(telefono):
    """Busca un cliente REAL por teléfono (últimos 7 dígitos, sin importar el
    formato guardado). Devuelve {'partner_id','nombre','pais','maquinas':[...]}
    o None si no se encuentra."""
    _connect()
    dig = re.sub(r"\D", "", telefono or "")
    if len(dig) < 7:
        return None
    sufijo = dig[-7:]
    # Este Odoo (v19) eliminó res.partner.mobile — solo "phone" existe de verdad
    # (mismo hallazgo que ya forzó a sync-contactos.js a detectar el campo en
    # runtime). Buscar por "mobile" tira ValueError de Odoo, no vacío.
    ids = _ex("res.partner", "search", [["phone", "like", sufijo]], limit=5)
    if not ids:
        return None
    p = _ex("res.partner", "read", ids, fields=["name", "country_id"])[0]
    pid = p["id"]
    maquinas = []
    oids = _ex("sale.order", "search", [["partner_id", "=", pid]], limit=10)
    if oids:
        line_ids = []
        for o in _ex("sale.order", "read", oids, fields=["order_line"]):
            line_ids += o.get("order_line") or []
        maquinas = _extraer_maquinas(line_ids)
    return {
        "partner_id": pid,
        "nombre": p.get("name") or "Cliente",
        "pais": (p["country_id"][1] if p.get("country_id") else ""),
        "maquinas": maquinas,
    }


def _contacto_sin_verificar(telefono):
    """Contacto mínimo para un caso que llegó SIN identidad verificada (sin el
    token del portal). No se engancha a un cliente real que coincida por
    teléfono: cualquiera puede decir un número ajeno. Lleva la categoría demo
    para no mezclarse con la cartera real; el equipo lo une si corresponde."""
    tel = (telefono or "").strip() or "sin teléfono"
    nombre = f"Cliente CeVi voz ({tel}) — sin verificar"
    ids = _ex("res.partner", "search", [["name", "=", nombre]], limit=1)
    if ids:
        return ids[0]
    return _ex(
        "res.partner", "create",
        {
            "name": nombre,
            "phone": telefono or False,
            "category_id": [(4, _state["cat_id"])],
            "comment": "Creado por CeVi (agente de voz) para un caso que llegó sin identidad verificada por el portal.",
        },
    )


def crear_ticket_voz(telefono, descripcion, ya_intento=None, serie=None, urgencia="normal",
                     motivo=None, partner_id=None, contexto=None, titulo=None):
    """Crea un ticket de Helpdesk REAL (equipo 'CeVi', etiqueta 'CeVi Voz') para
    las tools del agente de voz. Si viene partner_id (identidad verificada por el
    portal), el ticket va a la ficha de ese cliente; si no, a un contacto
    "sin verificar". `contexto` (modelo, serie, certificado) se agrega para que
    el técnico no tenga que volver a preguntar. Devuelve {'ticket','partner_id'}."""
    _connect()
    pid = int(partner_id) if partner_id else _contacto_sin_verificar(telefono)
    detalle = descripcion or ""
    if motivo:
        detalle = f"[{motivo}] " + detalle
    if ya_intento:
        detalle += f"\n\nYa intentó: {ya_intento}"
    if serie:
        detalle += f"\n\nNº de serie mencionado: {serie}"
    if contexto:
        detalle += f"\n\nDatos del portal: {contexto}"
    if not partner_id:
        detalle += "\n\n⚠️ Identidad NO verificada por el portal: confirmar quién es antes de dar datos."
    vals = {
        "name": (titulo or descripcion or "Caso desde CeVi voz")[:120],
        "partner_id": pid,
        "description": "".join(f"<p>{_esc(parrafo)}</p>" for parrafo in detalle.split("\n\n"))
                       + "<p><i>Generado por CeVi (agente de voz ElevenLabs).</i></p>",
        "team_id": _state["team_id"],
        "tag_ids": [(4, _state["voz_tag_id"])],
    }
    if urgencia == "alta":
        vals["priority"] = "2"
    tid = _ex("helpdesk.ticket", "create", vals)
    ref = None
    try:
        rec = _ex("helpdesk.ticket", "read", [tid], fields=["ticket_ref"])
        if rec:
            ref = rec[0].get("ticket_ref")
    except Exception:
        pass
    return {"ticket": ref or str(tid), "partner_id": pid}


# Etapas de Helpdesk dichas como las entiende el cliente.
_ETAPA_CLIENTE = {
    "New": "recibido, en cola para un técnico",
    "In Progress": "un técnico lo está revisando",
    "On Hold": "en espera (falta algo de tu lado o un repuesto)",
    "Solved": "resuelto",
    "Canceled": "cerrado sin acción",
}


def tickets_de_partner(partner_id, limite=5):
    """Casos del cliente con su estado en palabras simples: TODOS los abiertos
    (hasta 10) y los últimos cerrados. Antes eran solo los 5 más recientes y un
    caso abierto viejo podía quedar fuera de la lista (el agente decía que no
    existía)."""
    _connect()
    campos = ["ticket_ref", "name", "stage_id", "create_date", "write_date", "priority"]
    base = [["partner_id", "=", int(partner_id)]]
    abiertos = _ex("helpdesk.ticket", "search_read", base + [["stage_id.fold", "=", False]],
                   fields=campos, order="create_date desc", limit=10)
    cerrados = _ex("helpdesk.ticket", "search_read", base + [["stage_id.fold", "=", True]],
                   fields=campos, order="write_date desc", limit=3)
    recs = abiertos + cerrados
    out = []
    for r in recs:
        etapa = r["stage_id"][1] if r.get("stage_id") else ""
        out.append({
            "numero": r.get("ticket_ref") or str(r["id"]),
            "asunto": r.get("name") or "",
            "estado": _ETAPA_CLIENTE.get(etapa, etapa or "sin estado"),
            "abierto": etapa not in ("Solved", "Canceled"),
            "creado": (r.get("create_date") or "")[:10],
            "ultima_actualizacion": (r.get("write_date") or "")[:10],
            "urgente": r.get("priority") in ("2", "3"),
        })
    return out


_CRIT_NOMBRE = {
    "seguridad_respetada": "seguridad", "sin_comercial": "no comercial", "respuesta_fundada": "respuestas con respaldo",
    "cierre_util": "cierre útil", "no_pidio_identidad": "no pidió datos",
}


def nota_llamada_voz(partner_id, fila):
    """Nota interna en la ficha del cliente con el resumen de una llamada con
    CeVi (post-call webhook). Si algún criterio automático falló, lo marca para
    que el equipo revise esa conversación."""
    _connect()
    d = fila.get("datos") or {}
    fallos = [_CRIT_NOMBRE.get(k, k) for k, v in (fila.get("criterios") or {}).items() if v == "failure"]
    dur = fila.get("duracion_s")
    partes = [
        "<p><b>🎙️ Llamada con CeVi (asistente de voz)</b></p>",
        f"<p>{_esc(fila.get('resumen') or 'Sin resumen.')}</p>",
        "<ul>",
        f"<li><b>Motivo:</b> {_esc(str(d.get('motivo') or '—'))}</li>",
        f"<li><b>Síntoma:</b> {_esc(str(d.get('sintoma') or '—'))}</li>",
        f"<li><b>Resuelto en la llamada:</b> {'sí' if d.get('resuelto_en_llamada') else 'no'}</li>",
        f"<li><b>Caso creado:</b> {_esc(str(d.get('numero_caso') or '—'))}</li>",
        f"<li><b>Duración:</b> {dur // 60} min {dur % 60} s</li>" if isinstance(dur, int) else "",
        "</ul>",
    ]
    if d.get("riesgo_seguridad"):
        partes.append("<p>⚠️ <b>Se habló de un tema de seguridad.</b></p>")
    if fallos:
        partes.append(f"<p>🔎 <b>Revisar esta conversación</b> — falló la evaluación automática de: {_esc(', '.join(fallos))}.</p>")
    partes.append(f"<p><i>Conversación ElevenLabs: {_esc(fila.get('conversation_id') or '')}</i></p>")
    _ex("res.partner", "message_post", [int(partner_id)], body="".join(partes), subtype_xmlid="mail.mt_note")
