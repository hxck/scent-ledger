FROM python:3.12-slim

# Pillow needs these at build time for JPEG support; slim images don't ship them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable locations for the SQLite file and uploaded images.
# In production these should be mounted as volumes (see docker-compose.yml)
# so data survives container rebuilds.
RUN mkdir -p /app/data /app/static/uploads

ENV DATABASE_PATH=/app/data/scent_ledger.db
ENV UPLOAD_FOLDER=/app/static/uploads
ENV PORT=8000

# rembg (background removal) downloads its ONNX model on first use unless it's
# already cached here. Pre-downloading at build time means the container is
# fully self-contained at runtime — no outbound internet needed later, and no
# multi-second delay/failure risk on the first photo someone uploads.
# Non-fatal on purpose: if this step fails (network hiccup, etc.), the build
# still succeeds — the app already falls back to a download-on-first-use
# attempt, and ultimately to skipping background removal entirely, rather
# than blocking the whole deployment over what's an optional enhancement.
ENV U2NET_HOME=/app/.u2net
RUN python3 -c "from rembg import new_session; new_session('u2netp')" \
    || echo "WARNING: could not pre-download the rembg model at build time — will attempt on first use instead"

EXPOSE 8000

# Run as a non-root user
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
