# Build context is the repository root (see docker-compose.yml), so the image
# installs this checkout of celery-manager rather than a PyPI release.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE alembic.ini /src/
COPY src /src/src
# [standalone] brings uvicorn for the dashboard. kombu's built-in amqp
# transport is enough for RabbitMQ.
RUN pip install "/src[standalone]" "django>=5.0,<6.0" "celery>=5.3,<6.0" "redis>=5.0" "psycopg[binary]>=3.1"

COPY examples/django_demo /app

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
