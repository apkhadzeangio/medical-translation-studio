FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# LibreOffice converts the DOCX export to PDF; the fonts cover Georgian script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-noto-core fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

# Runs as root: a Railway volume is mounted root-owned, and the app must write uploads and
# extracted figures to it. The container is the isolation boundary.

# Migrations also run here, not only in Railway's preDeployCommand: if that step is skipped or
# overridden in the dashboard the app would otherwise start against an empty schema.
# `alembic upgrade head` is a no-op when the schema is current.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
