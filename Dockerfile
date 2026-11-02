FROM python:3.12-slim

# ffmpeg: merging video+audio and MP3 extraction. curl/unzip: to install Deno (JS runtime yt-dlp needs for YouTube).
RUN apt-get update && apt-get install -y ffmpeg curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# IMPORTANT: keep ONE worker (job progress lives in this process's memory),
# but use threads so one long download/file transfer does not block everyone else.
CMD ["sh", "-c", "gunicorn -w 1 --worker-class gthread --threads 8 -b 0.0.0.0:${PORT} app:app --timeout 600"]
