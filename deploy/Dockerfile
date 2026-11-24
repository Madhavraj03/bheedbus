# BheedBus on Hugging Face Spaces (Docker SDK). Built from the folder made by deploy/prepare_space.sh.
FROM python:3.10-slim

# ffmpeg: voice-note conversion; espeak-ng: spoken replies on Linux (macOS uses its own voices);
# fonts-dejavu-core: text on the WhatsApp map images
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

# Spaces run containers as user 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH \
    BHEEDBUS_READONLY=1 WHISPER_MODEL=small PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
# fetch the speech model at build time so the first voice note is quick
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

COPY --chown=user . .
EXPOSE 7860
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "7860"]
