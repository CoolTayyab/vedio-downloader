FROM python:3.12-slim

# ffmpeg is required for merging video+audio streams and MP3 extraction
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render/Railway inject $PORT at runtime; default to 5000 for local testing
ENV PORT=5000
EXPOSE 5000

CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT} app:app --timeout 120"]
