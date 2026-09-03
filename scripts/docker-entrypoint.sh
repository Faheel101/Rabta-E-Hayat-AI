#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

database_url="${DATABASE_URL:-sqlite:////data/rabta.db}"
first_boot=0

case "$database_url" in
  sqlite:///*)
    database_path="${database_url#sqlite:///}"
    if [ ! -s "$database_path" ]; then
      first_boot=1
    fi
    ;;
esac

if [ "$first_boot" -eq 1 ]; then
  echo "Rabta first boot: building deterministic synthetic demonstration data."
  python -m scripts.rebuild
else
  python -m scripts.migrate
fi

python -m scripts.release_check

exec python -m uvicorn web.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
