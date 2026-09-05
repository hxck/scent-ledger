FROM python:3.12-slim

# No build-time apt packages needed: Pillow ships a manylinux wheel with
# libjpeg and zlib already bundled, so the -dev packages this used to install
# were only adding size and build time.

WORKDIR /app

# Background removal on uploaded photos pulls in onnxruntime, opencv, scipy
# and numpy — roughly 300MB, against about 10MB for everything else here.
# It's optional: rembg is imported lazily and the app stores the photo
# unmodified if it's missing. Defaults to true so existing builds are
# unchanged; set to false for a dramatically smaller image.
ARG INSTALL_BG_REMOVAL=true

COPY requirements.txt requirements-bgremoval.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_BG_REMOVAL" = "true" ]; then \
         pip install --no-cache-dir -r requirements-bgremoval.txt; \
       fi

COPY . .

# Writable locations for the SQLite file and uploaded images.
# In production these should be mounted as volumes (see docker-compose.yml)
# so data survives container rebuilds.
RUN mkdir -p /app/data /app/static/uploads

ENV DATABASE_PATH=/app/data/scent_ledger.db
ENV UPLOAD_FOLDER=/app/static/uploads
ENV PORT=8000

# rembg downloads its ONNX model on first use unless it's already cached here.
# Pre-downloading at build time means the container is self-contained at
# runtime — no outbound internet needed later, and no multi-second delay or
# failure risk on the first photo someone uploads. Skipped entirely when
# background removal isn't installed.
#
# Non-fatal on purpose: if this step fails (network hiccup, etc.), the build
# still succeeds — the app already falls back to a download-on-first-use
# attempt, and ultimately to skipping background removal entirely, rather
# than blocking the whole deployment over an optional enhancement.
ENV U2NET_HOME=/app/.u2net
RUN if [ "$INSTALL_BG_REMOVAL" = "true" ]; then \
      python3 -c "from rembg import new_session; new_session('u2netp')" \
      || echo "WARNING: could not pre-download the rembg model at build time — will attempt on first use instead"; \
    fi

EXPOSE 8000

# Run as a non-root user
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
