FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data lives here — mount a volume at /app/data in Coolify
RUN mkdir -p /app/data/downloads
ENV DOWNLOAD_DIR=/app/data/downloads
ENV COOKIES_FILE=/app/data/cookies.txt

ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/')"

CMD gunicorn --bind "${HOST}:${PORT}" --workers 2 --timeout 360 app:app
