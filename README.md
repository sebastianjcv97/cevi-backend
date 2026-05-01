# CeVi Voice Backend

Mini-backend para el demo y futuro hardware ESP32 de CeVi Voice (asistente de voz integrado en máquinas láser C4V).

## Endpoints

- `POST /chat` — Claude Haiku 4.5 con system prompt CeVi + contexto cliente
- `POST /tts`  — Edge TTS Dalia mexicana (gratis, sin API key)
- `GET /health` — healthcheck

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
