FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 CARSCAN_CONFIG_DIR=/srv
WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY alembic.ini costs.yaml models.yaml ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /entrypoint.sh

EXPOSE 8000
CMD ["/entrypoint.sh"]
