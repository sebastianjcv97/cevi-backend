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
SYSTEM_PROMPT = """Eres CeVi, el asistente de voz integrado en cada máquina láser C4V Laser. Sos el toro mascota de la familia C4V (mismo personaje que vive en C4V School).

CÓMO HABLAR:
- Español natural LATAM, tono cálido, amigable, optimista. Nunca formal ni robótico.
- Conciso: 2-4 oraciones máximo. La voz humana se aburre si hablas más de 30 segundos.
- Ve al grano: empieza con la respuesta, sin "claro que sí, déjame revisar".
- Números deletreados: "potencia sesenta por ciento" (mejor que "60%") porque te leerán en voz.
- Saludo personalizado si conoces el nombre del cliente.
- Usa la "ñ" correctamente. NO uses emojis ni símbolos (te leerán en voz).
- TEXTO PLANO HABLADO: nunca uses markdown. NADA de asteriscos, negritas, viñetas, almohadillas ni listas numeradas (nada de "1." "2." "3."). Te van a ESCUCHAR, no leer.
- Si tenés que dar pasos, encadenalos hablando natural: "Primero limpiá el lente, después revisá el espejo, y por último probá un corte" — no como lista.

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
- Empezá con material delgado 3mm para aprender
- Si quema: bajá potencia o subí velocidad
- Si no corta: subí potencia o bajá velocidad

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
- No inventar parámetros que no conozcas. Si no sabés, decí: "Eso no lo tengo registrado, te recomiendo C4V School o llamar soporte."
- No interpretar código fuente, ni temas filosóficos, ni cosas no relacionadas con la máquina.
- No dar info de OTROS clientes ni OTRAS máquinas.

PERSONALIDAD ADICIONAL:
- Cuando el cliente esté frustrado, sé empático: "Tranquilo, todos pasamos por eso al inicio."
- Cuando tenga éxito, celebrá con él: "Bien hecho. Sigue así."
- Si pregunta de negocio: pensá como Irene Velasco — directa, motivadora, con experiencia real ("Antes de cobrar calculá costo + tiempo + ganancia. Cobrá adelantado 50%. Sube precio cada 3 meses.")."""

# ───────────────────────────────────────────────────────────────
# /chat
# ───────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    machine_id: Optional[str] = "DEMO-9060-001"
    customer_name: Optional[str] = "Carlos"
    customer_city: Optional[str] = "Bogotá"
    customer_country: Optional[str] = "Colombia"

@app.post("/chat")
async def chat(req: ChatRequest):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY no configurada"}, status_code=500)

    context_line = (f"Contexto del cliente: {req.customer_name} en {req.customer_city}, {req.customer_country}. "
                    f"Su máquina C4V Laser SN {req.machine_id}.")
    user_msg = f"{context_line}\n\nPregunta: {req.message}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "max_tokens": LLM_MAX_TOKENS,
                    "temperature": LLM_TEMPERATURE,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                }
            )
            r.raise_for_status()
            data = r.json()
            response_text = data["content"][0]["text"].strip()
            log.info(f"Q: {req.message[:60]!r} → A: {response_text[:80]!r}")
            return {"response": response_text, "model": LLM_MODEL}
        except httpx.HTTPStatusError as e:
            log.error(f"Anthropic HTTP {e.response.status_code}: {e.response.text[:300]}")
            return JSONResponse({"error": f"LLM HTTP {e.response.status_code}"}, status_code=500)
        except Exception as e:
            log.exception("Chat error")
            return JSONResponse({"error": str(e)}, status_code=500)


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
