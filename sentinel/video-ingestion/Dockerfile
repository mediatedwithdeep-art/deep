FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# ffmpeg is the decoder. No OpenCV: opencv-python is a ~90 MB wheel that
# mostly re-wraps ffmpeg and pulls GUI libraries a headless server does not
# need.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl && rm -rf /var/lib/apt/lists/*

COPY video-ingestion/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /app/shared/
RUN pip install --no-cache-dir -e /app/shared
COPY ai/ /app/ai/
COPY video-ingestion/ /app/video-ingestion/
COPY database/seeds/ /app/database/seeds/
COPY config/ /app/config/

RUN useradd --create-home --uid 10002 sentinel && chown -R sentinel /app
USER sentinel

ENV PYTHONPATH=/app/shared:/app/ai:/app/video-ingestion:/app/database/seeds
CMD ["python", "-u", "/app/video-ingestion/service.py"]
