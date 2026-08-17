# SUNI — container image
#
# Ollama is NOT in this image. It needs the GPU, is large, and is normally
# already running on the host or as its own service; bundling it would force a
# rebuild for every model change. Point SUNI at it with SUNI_OLLAMA_HOST —
# see docker-compose.yml.
#
#   docker build -t suni .
#   docker run -p 8765:8765 -v suni-data:/app/memory \
#     -e SUNI_OLLAMA_HOST=http://host.docker.internal:11434 suni
#
FROM python:3.12-slim AS base

# - fonts-dejavu-core: create_pdf needs a real TrueType font. Without it fpdf2
#   falls back to Latin-1 core fonts and silently mangles every accented
#   character, which is not acceptable for non-English output.
# - curl: HEALTHCHECK below.
# Kept to what the running app needs; build-only packages are not installed
# because every wheel in requirements.txt has a manylinux build.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      fonts-dejavu-core \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Never run as root: SUNI executes model-selected shell commands via run_shell,
# and that guard is a heuristic, not a sandbox.
RUN useradd --create-home --uid 10001 suni

WORKDIR /app

# Dependencies before source, so editing code does not re-run the slow layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional extras are deliberately NOT installed:
#   requirements-embeddings.txt  pulls torch (~2.5GB) for doc-KB search
#   requirements-imagegen.txt    pulls torch + diffusers
#   requirements-articles.txt    needs the unixODBC system library too
# Add them in a derived image if you want those features.

COPY --chown=suni:suni . .

# State lives here: databases, memory stores, uploads, the FAISS index and the
# generated secrets. Mount a volume or every restart starts from nothing.
#
# /app itself must be writable by the runtime user, not just its contents:
# create_app() creates the relative "files" output directory at startup, and
# WORKDIR made /app root-owned, so the container exited with
# "PermissionError: [Errno 13] Permission denied: 'files'".
#
# /app/certs is created HERE, owned by suni, on purpose: a named volume mounted
# at a path that exists in the image inherits that path's ownership, while one
# mounted at a path that does not exist is created root-owned — and then
# gen_cert.py below cannot write to it.
RUN mkdir -p /app/memory /app/logs /app/files /app/certs \
 && chown suni:suni /app /app/memory /app/logs /app/files /app/certs
VOLUME ["/app/memory", "/app/certs"]

USER suni

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SUNI_PORT=8765

EXPOSE 8765

# /api/auth/status is public and cheap, and it exercises config loading — so a
# healthy result means more than "the port is open".
#
# HTTPS is tried FIRST because the entrypoint below generates a certificate, so
# TLS is the normal case; --insecure because that certificate is self-signed.
# The HTTP fallback covers a deployment that deliberately removes the cert to
# sit behind a terminating proxy. Probing the wrong scheme first is not just
# untidy: a TLS handshake against a plaintext port makes uvicorn log "Invalid
# HTTP request received" every interval, which reads as an application fault.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS --insecure https://localhost:8765/api/auth/status \
      || curl -fsS http://localhost:8765/api/auth/status || exit 1

# web.py generates the self-signed certificate on first run, so nothing is
# needed here — the /app/certs volume above is what keeps it stable across
# restarts. Set SUNI_NO_TLS=1 to serve plaintext behind a terminating proxy.
CMD ["python", "web.py"]
