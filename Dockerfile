# quoth for local testing and evaluation: the package with the dense extra and the test
# suite. Models are not baked into the image: `quoth models fetch` fills the /models volume
# once (see compose.yaml).
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    QUOTH_HOME=/data \
    QUOTH_MODEL_CACHE=/models

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra dense --no-install-project

COPY src src
COPY eval eval
COPY hermes_plugin hermes_plugin
COPY web web
COPY tests tests
COPY scripts scripts
COPY .github .github
COPY deploy deploy
RUN uv sync --locked --extra dense
# Older Docker Desktop VM kernels lack SME support, and the OpenSSL bundled in recent
# cryptography wheels probes for it on Apple M4 hosts (SIGILL). Disable the probe in every
# Python process, including subprocesses started with a cleaned environment (MCP stdio).
RUN printf 'import os\nos.environ.setdefault("OPENSSL_armcap", "0")\n' \
    > /opt/venv/lib/python3.13/site-packages/sitecustomize.py

VOLUME ["/data", "/models"]
ENTRYPOINT ["quoth"]
CMD ["--help"]
