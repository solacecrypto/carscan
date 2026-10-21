FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 CARSCAN_CONFIG_DIR=/srv
# fewer malloc arenas: a threaded Python process otherwise grows with every thread
ENV MALLOC_ARENA_MAX=2
WORKDIR /srv

# pg_dump / pg_restore matching the managed Postgres (18) for the nightly backups
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY alembic.ini costs.yaml models.yaml ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
