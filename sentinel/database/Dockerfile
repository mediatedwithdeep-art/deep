# One-shot bootstrap: applies migrations, then seeds the demo estate.
# Runs to completion and exits; every other service waits on it.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install --no-cache-dir "psycopg[binary]>=3.2" "pydantic>=2.6" "pydantic-settings>=2.2"
COPY shared/ /app/shared/
COPY database/ /app/database/
ENV PYTHONPATH=/app/shared:/app/database/seeds
CMD ["sh", "-c", "python /app/database/migrate.py up && python /app/database/seeds/seed.py"]
