FROM python:3.12-slim

RUN pip install --no-cache-dir uv

RUN groupadd -r -g 1000 appgroup && useradd -r -u 1000 -g appgroup -s /bin/bash appuser

WORKDIR /app

COPY README.md ./
COPY pyproject.toml ./
COPY conductor/ ./conductor/
COPY server.py ./

RUN uv pip install --system --no-cache .

RUN mkdir -p /data && chown -R appuser:appgroup /app /data

USER appuser

EXPOSE 8093

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8093/health').read()" || exit 1

CMD ["conductor", "run"]