"""
Registro de conversaciones de voz de CeVi (c4v.cevi_conversaciones)
===================================================================
1) Al firmar la URL del widget (/voz/url-firmada) se pide a ElevenLabs un
   conversation_id asignado de antemano y se anota aquí A QUÉ CLIENTE pertenece,
   con la identidad ya verificada por el token del portal. Así la conversación
   queda atada al cliente del lado del servidor: nada de lo que mande el
   navegador (dynamic variables) sirve para cambiarlo.
2) Cuando termina la llamada, ElevenLabs manda el post-call webhook
   (/webhook/post-llamada): resumen, criterios de evaluación y datos extraídos.
   Se guardan en la misma fila y se deja una nota en la ficha de Odoo del
   cliente, para que el equipo vea qué habló con CeVi sin abrir ElevenLabs.

Muchas URLs firmadas nunca se usan: la que el widget gasta al leer su
configuración, y las de reserva que vencen sin llamada (el portal mantiene 2).
Esas filas quedan en estado 'url_emitida' y se borran a los 2 días (la purga
corre al arrancar y después cada 6 horas, al emitir una URL).
"""
import json
import logging
import os
import time

log = logging.getLogger("cevi.conversaciones")

DDL = """
create table if not exists c4v.cevi_conversaciones (
  conversation_id   text primary key,
  documento_norm    text,
  pais              text,
  odoo_partner_id   integer,
  estado            text not null default 'url_emitida',
  emitida_at        timestamptz not null default now(),
  terminada_at      timestamptz,
  duracion_s        integer,
  terminacion       text,
  resumen           text,
  motivo            text,
  sintoma           text,
  resuelto          boolean,
  requiere_humano   boolean,
  riesgo_seguridad  boolean,
  numero_caso       text,
  criterios         jsonb,
  datos             jsonb,
  tools             jsonb,
  nota_odoo         boolean default false
);
create index if not exists cevi_conv_partner on c4v.cevi_conversaciones (odoo_partner_id);
"""

_listo = False
_ultima_purga = 0.0
PURGA_CADA = 6 * 3600


def _conn():
    import psycopg
    return psycopg.connect(os.environ["POSTGRES_URL"], connect_timeout=5, autocommit=True)


def asegurar_tabla():
    global _listo, _ultima_purga
    if not os.environ.get("POSTGRES_URL"):
        return
    purgar = time.time() - _ultima_purga > PURGA_CADA
    if _listo and not purgar:
        return
    with _conn() as c:
        if not _listo:
            c.execute(DDL)
        c.execute("delete from c4v.cevi_conversaciones where estado = 'url_emitida' and emitida_at < now() - interval '2 days'")
    _listo, _ultima_purga = True, time.time()


def registrar_emision(conversation_id, doc, pais, partner_id):
    """Ata el conversation_id (aún no usado) al cliente verificado."""
    asegurar_tabla()
    with _conn() as c:
        c.execute(
            """insert into c4v.cevi_conversaciones (conversation_id, documento_norm, pais, odoo_partner_id)
               values (%s, %s, %s, %s) on conflict (conversation_id) do nothing""",
            (conversation_id, doc, pais, partner_id),
        )


def guardar_post_llamada(data):
    """Guarda lo que manda el post-call webhook. Devuelve la fila (con el
    partner) o None si la conversación no se inició con una URL nuestra."""
    asegurar_tabla()
    conv = data.get("conversation_id")
    meta = data.get("metadata") or {}
    an = data.get("analysis") or {}
    dc = {k: (v or {}).get("value") for k, v in (an.get("data_collection_results") or {}).items()}
    crit = {k: (v or {}).get("result") for k, v in (an.get("evaluation_criteria_results") or {}).items()}
    tools = []
    for turno in data.get("transcript") or []:
        for tr in turno.get("tool_results") or []:
            tools.append({"tool": tr.get("tool_name"), "error": bool(tr.get("is_error"))})
    with _conn() as c:
        fila = c.execute(
            """update c4v.cevi_conversaciones set
                 estado = 'terminada', terminada_at = now(),
                 duracion_s = %s, terminacion = %s, resumen = %s, motivo = %s, sintoma = %s,
                 resuelto = %s, requiere_humano = %s, riesgo_seguridad = %s, numero_caso = %s,
                 criterios = %s, datos = %s, tools = %s
               where conversation_id = %s
               returning odoo_partner_id, documento_norm, nota_odoo""",
            (
                meta.get("call_duration_secs"), meta.get("termination_reason"), an.get("transcript_summary"),
                dc.get("motivo"), dc.get("sintoma"), dc.get("resuelto_en_llamada"), dc.get("requiere_humano"),
                dc.get("riesgo_seguridad"), dc.get("numero_caso"),
                json.dumps(crit), json.dumps(dc), json.dumps(tools), conv,
            ),
        ).fetchone()
    if not fila:
        return None
    return {"conversation_id": conv, "partner_id": fila[0], "doc": fila[1], "nota_odoo": fila[2],
            "resumen": an.get("transcript_summary") or "", "criterios": crit, "datos": dc,
            "duracion_s": meta.get("call_duration_secs"), "tools": tools}


def marcar_nota(conversation_id):
    with _conn() as c:
        c.execute("update c4v.cevi_conversaciones set nota_odoo = true where conversation_id = %s", (conversation_id,))
