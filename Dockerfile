# Author: Maurizio Verde — LastReload
# Proxy + dashboard in one container. The dashboard keeps its loopback-only bind (the prompt never leaves the
# machine), so on Linux reach it with `docker run --network=host`; the proxy is published on PROXY_HOST and
# requires the API key.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ctxproxy/ ctxproxy/
COPY examples/ examples/
COPY dashboard/ dashboard/
RUN pip install --no-cache-dir .

ENV UPSTREAM=http://127.0.0.1:8095 \
    API_KEY= \
    CLIENT_KEY= \
    PROXY_HOST=0.0.0.0 \
    PROXY_PORT=8096 \
    PROXY_ARGS= \
    DATA=/data \
    DASH_HOST=127.0.0.1 \
    DASH_PORT=8097

EXPOSE 8096
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/health', timeout=4)" || exit 1

COPY docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
