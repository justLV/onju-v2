"""
Embedded ASR server using parakeet-mlx (Apple Silicon).

Run as a separate process:
    python -m pipeline.services.asr_server
    python -m pipeline.services.asr_server --port 8100 --model mlx-community/parakeet-tdt-0.6b-v3

Install dependencies:
    uv pip install -e ".[asr]"
"""

import logging
import os
import tempfile
import time
import traceback

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse

MODEL_ID = os.environ.get("ASR_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("parakeet")

app = FastAPI()
model = None


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception on %s %s\n%s",
        request.method,
        request.url.path,
        traceback.format_exc(),
    )
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.on_event("startup")
async def load_model():
    global model
    from parakeet_mlx import from_pretrained

    tic = time.time()
    try:
        model = from_pretrained(MODEL_ID)
        logger.info("Model %s loaded in %.1fs", MODEL_ID, time.time() - tic)
    except Exception:
        logger.error("Failed to load model %s\n%s", MODEL_ID, traceback.format_exc())
        raise


@app.get("/health")
async def health():
    return {"status": "ok" if model else "loading", "model": MODEL_ID}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    raw = await audio.read()

    ext = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
        f.write(raw)
        tmp_path = f.name

    try:
        tic = time.time()
        result = model.transcribe(tmp_path)
        elapsed = time.time() - tic
    except Exception:
        logger.error(
            "Transcription failed for file %s (ext=%s)\n%s",
            audio.filename,
            ext,
            traceback.format_exc(),
        )
        raise
    finally:
        os.unlink(tmp_path)

    text = result.text.strip()
    duration_s = result.sentences[-1].end if result.sentences else 0.0

    return {
        "text": text,
        "duration_s": round(duration_s, 2),
        "transcribe_time_s": round(elapsed, 3),
    }


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Parakeet ASR server")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--model", default=None, help="Override ASR_MODEL env var")
    args = parser.parse_args()

    if args.model:
        MODEL_ID = args.model

    uvicorn.run(app, host=args.host, port=args.port)
