FROM python:3.11-slim

# Imposta variabili d'ambiente per Python e fuso orario italiano
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Rome

WORKDIR /app

# Installa le dipendenze di sistema necessarie per Discord Voice, FFmpeg, Opus, NodeJS (per yt-dlp-ejs) e SpeechRecognition
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libsodium23 \
    libffi-dev \
    flac \
    nodejs \
    git \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Installa prima le dipendenze Python per sfruttare la cache dei layer Docker
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copia il codice sorgente del bot
COPY . .

# Avvia VenusBot
CMD ["python", "venusbot.py"]
