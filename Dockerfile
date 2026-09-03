# syntax=docker/dockerfile:1.7

FROM node:22-alpine AS assets
WORKDIR /build
COPY package.json package-lock.json tailwind.config.js ./
RUN npm ci --ignore-scripts
COPY assets ./assets
COPY web/templates ./web/templates
RUN npm run css

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .
COPY --from=assets /build/web/static/css/app.css /app/web/static/css/app.css

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin rabta \
    && mkdir -p /data /backups \
    && chown -R rabta:rabta /app /data /backups \
    && chmod 0755 /app/scripts/docker-entrypoint.sh

USER rabta
EXPOSE 8000
VOLUME ["/data", "/backups"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)" || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
