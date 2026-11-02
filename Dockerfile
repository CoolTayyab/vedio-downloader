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

# IMPORTANT: only 1 worker. Download progress is tracked in an in-memory
# dict shared across requests — with 2+ workers, a "start download" request
# and a later "check progress" request can land on different worker
# processes that don't share memory, causing "Unknown job" errors.
# A single worker keeps all requests on the same process so progress
# tracking works correctly. (If this app ever needs to scale to handle
# many simultaneous users, the fix is to move job state into something
# shared like Redis instead of adding more workers.)
CMD ["sh", "-c", "gunicorn -w 1 -b 0.0.0.0:${PORT} app:app --timeout 120"]
