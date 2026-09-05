FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY event-processor/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared
COPY ai/ /app/ai/
COPY event-processor/ /app/event-processor/

RUN useradd --create-home --uid 10003 sentinel && chown -R sentinel /app
USER sentinel

ENV PYTHONPATH=/app/shared:/app/ai:/app/event-processor
CMD ["python", "-u", "/app/event-processor/service.py"]
