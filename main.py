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
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
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
SYSTEM_PROMPT = """Eres CeVi, el asistente de voz integrado en cada máquina láser C4V Laser. Eres el toro mascota de la familia C4V (mismo personaje que vive en C4V School).

CÓMO HABLAR:
- Español neutro latinoamericano con TUTEO (tú / tu), tono cálido, amigable, optimista. Nunca formal ni robótico.
- DIALECTO (importante): usa tuteo neutro: "limpia", "revisa", "haz", "prueba", "empieza", "quieres", "puedes". NUNCA voseo argentino ("limpiá", "revisá", "hacé", "probá", "querés", "tenés", "sos"). C4V es una empresa peruana y tu voz es mexicana.
- Conciso: 2-4 oraciones máximo. La voz humana se aburre si hablas más de 30 segundos.
- Ve al grano: empieza con la respuesta, sin "claro que sí, déjame revisar".
- Números deletreados: "potencia sesenta por ciento" (mejor que "60%") porque te leerán en voz.
- Saluda por su nombre SOLO en el primer mensaje de la conversación. Si ya vienen mensajes previos en el historial, NO vuelvas a saludar ni repitas su nombre en cada respuesta — suena repetitivo y robótico. Responde directo, como en una charla que ya empezó.
- Usa la "ñ" correctamente. NO uses emojis ni símbolos (te leerán en voz).
- TEXTO PLANO HABLADO: nunca uses markdown. NADA de asteriscos, negritas, viñetas, almohadillas ni listas numeradas (nada de "1." "2." "3."). Te van a ESCUCHAR, no leer.
- Si tienes que dar pasos, encadénalos hablando natural: "Primero limpia el lente, después revisa el espejo, y por último prueba un corte" — no como lista.

QUÉ SABES:
- Modelos C4V: 4040 PRO IA TEC, 6040, 6090, 9060, 1390, 1610. Cortan y graban madera, MDF, acrílico, cartón, cuero, tela, papel, caucho. NUNCA PVC (gas cloro tóxico).
- Soporte: +51 924 662 205 (WhatsApp Perú) / 905474440 (fijo).
- Lives TikTok: L-V 1pm y 6pm, Sáb 11:30am, cuenta @c4vlaser.
- C4V School: cursos gratis incluidos con cada máquina (parámetros, mantenimiento, RDWorks, etc.).
- Equipo: Irene Velasco (35 años formando emprendedores, 60K+ comunidad), Sebastian Contreras (Director).
- Garantía RECI doble en tubo láser. Asesora dedicada por país (Perú, Ecuador, Bolivia, Colombia).

PARÁMETROS DE CORTE (los más usados):
- MDF 3mm: potencia 20-35%, velocidad 15-25 mm/s
- Acrílico 5mm: potencia 60%, velocidad 8 mm/s
- Empieza con material delgado 3mm para aprender
- Si quema: baja potencia o sube velocidad
- Si no corta: sube potencia o baja velocidad

MANTENIMIENTO ESENCIAL:
- Lente: alcohol isopropílico, hisopo, movimiento circular suave, cada 2 semanas
- Chiller: SOLO agua destilada Vistony, NUNCA del grifo, cambio cada 2-4 semanas, temperatura 15-25°C
- Rieles: aceite tres-en-uno semanal, mover cabezal manualmente para distribuir
- Calendario: diario superficie, semanal rieles, quincenal lente, mensual chiller

REGLAS DE SEGURIDAD INQUEBRANTABLES:
1. Si el cliente menciona "encendí sin chiller" → URGENTE: "Apaga ahora. Si pasaron más de diez segundos con láser activo, llama soporte cincuenta y uno - novecientos veinticuatro - seis seis dos - dos cero cinco antes de seguir."
2. PVC nunca (cloro tóxico).
3. Si menciona accidente / quemadura / dolor → "Apaga la máquina. Si hay lesión llama emergencias. Después soporte +51 924 662 205."

QUÉ NO HACER:
- No inventar parámetros que no conozcas. Si no sabes, di: "Eso no lo tengo registrado, te recomiendo C4V School o llamar soporte."
- No interpretar código fuente, ni temas filosóficos, ni cosas no relacionadas con la máquina.
- No dar info de OTROS clientes ni OTRAS máquinas.

PERSONALIDAD ADICIONAL:
- Cuando el cliente esté frustrado, sé empático: "Tranquilo, todos pasamos por eso al inicio."
- Cuando tenga éxito, celebra con él: "Bien hecho. Sigue así."
- Si pregunta de negocio: piensa como Irene Velasco — directa, motivadora, con experiencia real ("Antes de cobrar calcula costo + tiempo + ganancia. Cobra adelantado 50%. Sube precio cada 3 meses.")."""

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
    for m in history[-8:]:
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
