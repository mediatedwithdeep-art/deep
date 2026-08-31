# Build context is the sentinel/ root, because every Python service needs
# shared/sentinel_core. Copying it per service keeps the images independent
# with no registry or private index required.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared

COPY backend/app/ /app/app/
COPY database/ /app/database/

# Never run as root. A container escape from a service that parses
# attacker-influenced data (camera names, plate text) should not land on a
# root shell.
RUN useradd --create-home --uid 10001 sentinel && chown -R sentinel /app
USER sentinel

ENV PYTHONPATH=/app:/app/shared
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
