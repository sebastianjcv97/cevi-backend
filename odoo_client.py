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

# ── Casos de CeVi voz con la estructura del equipo (decisión 23-set-2026) ──
# Van al tablero que el equipo ya trabaja, "Atención al cliente" de C4V LASER
# (helpdesk.team 12), SIN asignar (lo reparte la coordinación), con UNA
# etiqueta de tipo como las del equipo + "CeVi Voz" para saber el origen, el
# título en mayúsculas ("SOPORTE TECNICO - 13100U - ...") y la ficha del
# cliente en la descripción. Las PRUEBAS (contacto verificado con la categoría
# "CeVi Demo", como Martín) siguen en el tablero "CeVi" (14).
# Lo comercial no es un caso: es una oportunidad en el CRM (Ventas Perú,
# Bolivia o Ecuador), donde trabajan los asesores.
EQUIPO_REAL = int(os.environ.get("CEVI_EQUIPO_REAL") or 12)
SIMULAR = os.environ.get("CEVI_ODOO_SIMULAR") == "1"   # solo registra en el log lo que crearía
ETIQUETA_TIPO = {
    "soporte": "SOPORTE TECNICO", "seguridad": "SEGURIDAD", "cotizacion": "COTIZACION",
    "revision": "SOPORTE TECNICO", "instalacion_y_capacitacion": "INSTALACION Y CAPACITACION",
    "capacitacion": "CAPACITACION VIRTUAL", "mantenimiento": "MANTENIMIENTO PREVENTIVO",
    "repuesto": "INSTALACION REPUESTO",
}
ETIQUETAS_NUEVAS = {"SOPORTE TECNICO", "SEGURIDAD"}   # creación aprobada por Sebastián (23-set-2026)
CRM_EQUIPO = {"PE": 5, "BO": 7, "EC": 6}               # Ventas Peru / Ventas Bolivia / Ventas Ecuador
CRM_EMPRESA = {"BO": 3, "EC": 4}                       # Perú: la empresa que le vendió (1 o 13); si no, 1

_lock = threading.Lock()
_state = {
    "uid": None, "models": None, "team_id": None, "hd_tag_id": None, "cat_id": None,
    "voz_tag_id": None, "tipo_tags": {}, "equipos": {},
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
    try:
        _setup_estructura()
    except Exception:
        log.exception("Odoo: no se pudo leer la estructura del tablero; los casos salen sin etiqueta de tipo")


def _setup_estructura():
    """Etiquetas de tipo (solo busca las del equipo; crea las dos aprobadas) y,
    por tablero, su empresa y las propiedades FECHA DE ENTREGA y CIUDAD."""
    tags = {}
    for nombre in set(ETIQUETA_TIPO.values()):
        ids = _ex("helpdesk.tag", "search", [["name", "=", nombre]], limit=1)
        if not ids and nombre in ETIQUETAS_NUEVAS:
            ids = [_ex("helpdesk.tag", "create", {"name": nombre})]
        if ids:
            tags[nombre] = ids[0]
    _state["tipo_tags"] = tags
    for equipo in {EQUIPO_REAL, _state["team_id"]}:
        rec = _ex("helpdesk.team", "read", [equipo], fields=["company_id", "ticket_properties"])
        if not rec:
            continue
        props = {str(d.get("string", "")).upper(): d.get("name") for d in rec[0].get("ticket_properties") or []}
        _state["equipos"][equipo] = {
            "empresa": (rec[0].get("company_id") or [None])[0],
            "entrega": props.get("FECHA DE ENTREGA"), "ciudad": props.get("CIUDAD"),
        }


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


def es_prueba(partner_id) -> bool:
    """Contacto VERIFICADO con la categoría "CeVi Demo" (p. ej. Martín): sus
    casos van al tablero de pruebas, nunca al del equipo ni al CRM."""
    if not partner_id:
        return False
    _connect()
    rec = _ex("res.partner", "read", [int(partner_id)], fields=["category_id"])
    return bool(rec) and _state["cat_id"] in (rec[0].get("category_id") or [])


def _vinculos(maquina, empresa):
    """Producto, serie (lote) y pedido de la máquina del cliente, solo los que
    Odoo acepta en un caso de ese tablero: el pedido tiene que ser de la misma
    empresa y el lote, del mismo producto."""
    v = {}
    if not maquina:
        return v
    try:
        cod = re.match(r"\s*\[([^\]]+)\]", str(maquina.get("producto") or ""))
        if cod:
            ids = _ex("product.product", "search", [["default_code", "=", cod.group(1)]], limit=1)
            if ids:
                v["product_id"] = ids[0]
        if maquina.get("serie") and v.get("product_id"):
            dom = [["name", "=", maquina["serie"]], ["product_id", "=", v["product_id"]]]
            if empresa:
                dom += ["|", ["company_id", "=", False], ["company_id", "=", empresa]]
            ids = _ex("stock.lot", "search", dom, limit=1)
            if ids:
                v["lot_id"] = ids[0]
        if maquina.get("pedido") and empresa:
            ids = _ex("sale.order", "search", [["name", "=", maquina["pedido"]], ["company_id", "=", empresa]], limit=1)
            if ids:
                v["sale_order_id"] = ids[0]
    except Exception:
        log.exception("Odoo: no se pudieron buscar producto/serie/pedido")
    return v


def caso_reciente(conversation_id, titulo):
    """Si en esta conversación ya se creó este mismo caso en los últimos 30 min
    (aunque el backend se haya reiniciado), devuelve su número."""
    if not conversation_id:
        return None
    _connect()
    desde = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 1800))
    rec = _ex("helpdesk.ticket", "search_read",
              [["description", "ilike", conversation_id], ["name", "=", titulo], ["create_date", ">=", desde]],
              fields=["ticket_ref"], limit=1)
    return (rec[0].get("ticket_ref") or str(rec[0]["id"])) if rec else None


def crear_caso_voz(tipo, titulo, cuerpo_html, prioridad="0", partner_id=None, telefono=None,
                   ciudad=None, maquina=None):
    """Caso de Helpdesk como los crea el equipo (ver arriba). Devuelve
    {'ticket', 'partner_id', 'equipo'}. Si Odoo rechaza los vínculos o las
    propiedades (p. ej. empresas cruzadas), se crea igual sin ellos."""
    _connect()
    prueba = es_prueba(partner_id)
    pid = int(partner_id) if partner_id else _contacto_sin_verificar(telefono)
    equipo = _state["team_id"] if prueba else EQUIPO_REAL
    info = _state["equipos"].get(equipo, {})
    etiquetas = [_state["voz_tag_id"]]
    tipo_tag = _state["tipo_tags"].get(ETIQUETA_TIPO.get(tipo, "SOPORTE TECNICO"))
    if tipo_tag:
        etiquetas.insert(0, tipo_tag)
    if prueba:
        etiquetas.append(_state["hd_tag_id"])
    vals = {"name": titulo, "partner_id": pid, "team_id": equipo, "description": cuerpo_html,
            "tag_ids": [(6, 0, etiquetas)], "priority": prioridad, "user_id": False}
    extra = _vinculos(maquina, info.get("empresa"))
    props = {}
    if info.get("entrega") and (maquina or {}).get("fecha_entrega"):
        props[info["entrega"]] = str(maquina["fecha_entrega"])[:10]
    if info.get("ciudad") and ciudad:
        props[info["ciudad"]] = str(ciudad).upper()
    if props:
        extra["properties"] = props
    if SIMULAR:
        log.info("SIMULAR helpdesk.ticket.create %s", {**vals, **extra})
        return {"ticket": "SIMULADO", "partner_id": pid, "equipo": equipo}
    try:
        tid = _ex("helpdesk.ticket", "create", {**vals, **extra})
    except Exception:
        log.exception("Odoo rechazó el caso con vínculos/propiedades %s; se crea sin ellos", list(extra))
        tid = _ex("helpdesk.ticket", "create", vals)
    ref = None
    try:
        rec = _ex("helpdesk.ticket", "read", [tid], fields=["ticket_ref"])
        ref = rec[0].get("ticket_ref") if rec else None
    except Exception:
        pass
    return {"ticket": ref or str(tid), "partner_id": pid, "equipo": equipo}


def _origen_crm(nombre):
    """Fuente (utm.source) con ese nombre; se crea la primera vez. Sirve para
    contar en el CRM cuántas oportunidades trae cada CeVi."""
    if not nombre:
        return None
    if nombre not in _state.setdefault("origenes", {}):
        _state["origenes"][nombre] = _find_or_create("utm.source", [["name", "=", nombre]], {"name": nombre})
    return _state["origenes"][nombre]


def _empresa_de_maquina(maquina):
    """Empresa que le vendió la máquina: la de la ficha o, si no está, la del pedido."""
    emp = (maquina or {}).get("empresa_id")
    if emp:
        return emp
    if (maquina or {}).get("pedido"):
        rec = _ex("sale.order", "search_read", [["name", "=", maquina["pedido"]]], fields=["company_id"], limit=1)
        if rec and rec[0].get("company_id"):
            return rec[0]["company_id"][0]
    return None


def crear_oportunidad_voz(nombre, cuerpo_html, partner_id=None, telefono=None, pais="PE", maquina=None,
                          origen="CeVi soporte"):
    """Consulta comercial → oportunidad en el CRM (etapa New, asignada al líder
    del equipo de ventas del país), como las que cargan los asesores. `origen` va
    como fuente (utm.source) para distinguir de dónde vino ("CeVi soporte",
    "CeVi web ventas"). Devuelve {'ticket', 'partner_id'}."""
    _connect()
    pid = int(partner_id) if partner_id else _contacto_sin_verificar(telefono)
    pais = (pais or "PE").upper()[:2]
    emp_maq = _empresa_de_maquina(maquina)
    empresa = CRM_EMPRESA.get(pais) or (emp_maq if emp_maq in (1, 13) else 1)
    equipo = CRM_EQUIPO.get(pais, 5)
    # El tablero del CRM abre filtrado por "asignadas a mí" y no hay asignación
    # automática: sin responsable, ninguna vendedora la vería. Va al líder del
    # equipo de ventas (en Perú, Jose), que la reparte; si no tiene, sin asignar.
    lider = _ex("crm.team", "read", [equipo], fields=["user_id"])
    responsable = (lider[0].get("user_id") or [False])[0] if lider else False
    vals = {"name": nombre, "type": "opportunity", "partner_id": pid, "team_id": equipo,
            "company_id": empresa, "user_id": responsable, "description": cuerpo_html}
    fuente = _origen_crm(origen) if not SIMULAR else None
    if fuente:
        vals["source_id"] = fuente
    if SIMULAR:
        log.info("SIMULAR crm.lead.create %s", vals)
        return {"ticket": "SIMULADO", "partner_id": pid}
    lid = _ex("crm.lead", "create", vals)
    return {"ticket": f"oportunidad {lid}", "partner_id": pid}


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


# ── CeVi comercial (página pública /ventas) ─────────────────────────────────
# Decisiones del dueño (24-set-2026), sobre cómo trabaja hoy el CRM: las
# vendedoras cargan cada oportunidad al cotizar, asignada a sí mismas, y el
# tablero abre filtrado por "asignadas a mí" (sin asignación automática). Así
# que el prospecto de CeVi va a Melva, en C4V LASER S.C.R.L. (la empresa que
# usan en setiembre; la del usuario API es KUY y ellas no la ven), equipo
# Ventas Peru, con la etiqueta "CeVi voz" y la fuente "CeVi web ventas" para
# distinguirlo y medir. Sin contacto (res.partner): la vendedora lo liga con el
# DNI/RUC al cotizar; aquí solo van nombre y teléfono.
VENTAS_VENDEDORA = int(os.environ.get("CEVI_VENTAS_VENDEDORA") or 9)      # Melva
VENTAS_EMPRESA = int(os.environ.get("CEVI_VENTAS_EMPRESA") or 13)         # C4V LASER S.C.R.L.
VENTAS_EQUIPO = 5                                                         # Ventas Peru
VENTAS_ETIQUETA = "CeVi voz"
VENTAS_FUENTE = "CeVi web ventas"
VENTAS_MEDIO = 12                                                         # utm.medium "Chat en vivo"
VENTAS_PAISES = {"PE": 173, "EC": 63, "BO": 29}
_INTERES_TXT = {"comprar": "quiere comprar", "cotizar": "pide cotización", "formas_de_pago": "pregunta formas de pago",
                "envio": "pregunta por el envío", "visita": "quiere visitar / ver la máquina", "taller": "le interesan los talleres",
                "informacion": "pide más información"}


def _tel_legible(tel):
    """'+51995547575' → '+51 995 547 575' (como los carga el equipo)."""
    d = "".join(ch for ch in (tel or "") if ch.isdigit())
    for pref in ("593", "591", "51", "56", "57"):
        if d.startswith(pref):
            resto = d[len(pref):]
            return f"+{pref} " + " ".join(resto[i:i + 3] for i in range(0, len(resto), 3))
    return tel


def crear_lead_voz(nombre, telefono, pais=None, ciudad=None, rubro=None, etapa=None, modelo_interes=None,
                   interes=None, resumen=None, email=None, conversation_id=None):
    """Prospecto del CeVi comercial → oportunidad en el CRM, visible para la
    vendedora. Si el mismo número dejó datos en los últimos 30 días, no
    duplica: agrega una nota a esa oportunidad. Devuelve {'lead_id', 'existia'}."""
    _connect()
    ctx = {"allowed_company_ids": [VENTAS_EMPRESA], "lang": "es_PE"}
    tel = _tel_legible(telefono)
    previos = _ex("crm.lead", "search", [["phone", "in", list({tel, telefono})], ["type", "=", "opportunity"],
                                          ["create_date", ">=", time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))]],
                  limit=1, context=ctx)
    detalle = [
        f"<li><b>Qué quiere:</b> {_esc(_INTERES_TXT.get(interes, interes or '—'))}</li>",
        f"<li><b>Qué va a producir / rubro:</b> {_esc(rubro or '—')}</li>",
        f"<li><b>Etapa:</b> {_esc(etapa or '—')}</li>",
        f"<li><b>Modelo de interés:</b> {_esc(modelo_interes or '—')}</li>",
        f"<li><b>País / ciudad:</b> {_esc(pais or '—')} / {_esc(ciudad or '—')}</li>",
        f"<li><b>Correo:</b> {_esc(email)}</li>" if email else "",
    ]
    cuerpo = ("<p><b>🎙️ Interesado desde CeVi (página pública de ventas)</b></p><ul>" + "".join(detalle) + "</ul>"
              f"<p>{_esc(resumen or '')}</p>"
              + (f"<p><i>Conversación ElevenLabs: {_esc(conversation_id)}</i></p>" if conversation_id else ""))
    if previos:
        if not SIMULAR:
            _ex("crm.lead", "message_post", previos, body=cuerpo, subtype_xmlid="mail.mt_note", context=ctx)
        return {"lead_id": previos[0], "existia": True}
    etiqueta = None if SIMULAR else _find_or_create("crm.tag", [["name", "=", VENTAS_ETIQUETA]], {"name": VENTAS_ETIQUETA})
    vals = {
        "name": f"{modelo_interes} · CeVi voz" if modelo_interes else f"CeVi voz · {nombre}",
        "type": "opportunity", "contact_name": nombre, "phone": tel, "email_from": email or False,
        "team_id": VENTAS_EQUIPO, "company_id": VENTAS_EMPRESA, "user_id": VENTAS_VENDEDORA, "stage_id": 1,
        "lang_id": 78, "country_id": VENTAS_PAISES.get((pais or "").upper(), False), "city": ciudad or False,
        "tag_ids": [(6, 0, [etiqueta])] if etiqueta else False,
        "source_id": None if SIMULAR else _origen_crm(VENTAS_FUENTE), "medium_id": VENTAS_MEDIO,
        "priority": "1" if interes in ("comprar", "cotizar") else "0",
        "material": (rubro or "")[:120] or False, "description": cuerpo,
    }
    if SIMULAR:
        log.info("SIMULAR crm.lead.create (ventas) %s", vals)
        return {"lead_id": 0, "existia": False}
    return {"lead_id": _ex("crm.lead", "create", vals, context=ctx), "existia": False}


def nota_lead_voz(lead_id, fila):
    """Resumen post-llamada del CeVi comercial en la oportunidad creada en esa
    conversación (lo que se habló después de dejar los datos incluido)."""
    _connect()
    d = fila.get("datos") or {}
    dur = fila.get("duracion_s")
    fallos = [k for k, v in (fila.get("criterios") or {}).items() if v == "failure"]
    partes = [
        "<p><b>🎙️ Conversación con CeVi (ventas)</b></p>",
        f"<p>{_esc(fila.get('resumen') or 'Sin resumen.')}</p><ul>",
        f"<li><b>Interés:</b> {_esc(str(d.get('nivel_interes') or '—'))}</li>",
        f"<li><b>Modelo recomendado:</b> {_esc(str(d.get('modelo_recomendado') or '—'))}</li>",
        f"<li><b>Preguntó precio:</b> {'sí' if d.get('pidio_precio') else 'no'}</li>",
        f"<li><b>Duración:</b> {dur // 60} min {dur % 60} s</li>" if isinstance(dur, int) else "",
        "</ul>",
    ]
    if fallos:
        partes.append(f"<p>🔎 <b>Revisar esta conversación</b> — falló la evaluación automática de: {_esc(', '.join(fallos))}.</p>")
    partes.append(f"<p><i>Conversación ElevenLabs: {_esc(fila.get('conversation_id') or '')}</i></p>")
    _ex("crm.lead", "message_post", [int(lead_id)], body="".join(partes), subtype_xmlid="mail.mt_note",
        context={"allowed_company_ids": [VENTAS_EMPRESA]})
