#!/bin/sh
# One image, two roles. ROLE=web (default) serves the site, ROLE=worker runs the scrapers + bot.
set -e
case "${ROLE:-web}" in
  web|all)
    alembic upgrade head
    exec uvicorn app.web.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*' --no-access-log
    ;;
  worker)
    exec python -m app.worker.main
    ;;
  *)
    echo "unknown ROLE=$ROLE (web|worker|all)" >&2
    exit 1
    ;;
esac
