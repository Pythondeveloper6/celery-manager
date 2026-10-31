# Built from the repository root so the local checkout is what gets installed:
#   docker compose -f examples/docker/compose.yaml up --build
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 app
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[redis,standalone]" "psycopg[binary]>=3.1"
WORKDIR /app
COPY examples/docker/project.py ./
# beat keeps its local schedule state (celerybeat-schedule) in the workdir.
RUN chown app:app /app
USER app
EXPOSE 8000
