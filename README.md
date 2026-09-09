# CeVi Voice Backend

Mini-backend para el demo y futuro hardware ESP32 de CeVi Voice (asistente de voz integrado en máquinas láser C4V).

## Endpoints

- `POST /chat` — Claude Haiku 4.5 con system prompt CeVi + contexto cliente.
  Devuelve la respuesta completa en un JSON. Puede crear tickets en Odoo.
  **No lo cambies:** ManyChat y el hardware dependen de esta forma.
- `POST /chat/stream` — lo mismo, pero devuelve **SSE** con un evento `frase`
  por cada oración en cuanto Claude la escribe, y un `fin` con el texto entero.
  Es lo que usa el portal web: le permite empezar a hablar sin esperar a que el
  modelo termine (de ~2.400 ms a ~700 ms hasta la primera palabra).
  No usa herramientas, porque crear un ticket obliga a una segunda vuelta al
  modelo y rompería el flujo frase a frase.
- `POST /tts`  — Edge TTS Dalia mexicana (gratis, sin API key)
- `GET /health` — healthcheck

## ⚠️ Riesgo conocido: Edge TTS

`edge-tts` es una **API interna de Microsoft usada por ingeniería inversa**, no
un producto con contrato. Ha tenido cortes por 403 masivos y desde diciembre de
2025 exige cabeceras nuevas. **El día que Microsoft la cierre, CeVi se queda
muda sin aviso.** Antes de depender de esto para muchos clientes conviene dejar
listo un respaldo de pago (Azure Neural `es-PE Camila` ≈ 16 USD por millón de
caracteres, o Deepgram Aura-2 `es-MX Estrella` ≈ 30 USD). Con el volumen
previsto (200 clientes, 5 conversaciones de 3 min al mes) el respaldo costaría
entre 21 y 39 USD al mes, y solo si el primario cae.

## Variables de entorno

```
ANTHROPIC_API_KEY=sk-ant-...    # generada en console.anthropic.com
ALLOWED_ORIGIN=*                # opcional; en prod restringir al dominio
```

## Local

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
uvicorn main:app --reload
# http://localhost:8000/health
```

## Deploy a Railway

1. New Project → Deploy from GitHub repo → seleccionar `cevi-backend`
2. Variables → agregar `ANTHROPIC_API_KEY`
3. Deploy automático
4. URL pública en Settings → Networking → Generate Domain
