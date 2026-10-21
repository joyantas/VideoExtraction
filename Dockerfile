FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=3000 \
    DATA_DIR=/app/data \
    PYTHONPATH=/app/src
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app
COPY requirements.txt pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','3000')+'/healthz', timeout=2)" || exit 1
CMD ["sh", "-c", "exec gunicorn -w 4 -k uvicorn_worker.UvicornWorker --bind 0.0.0.0:${PORT:-3000} --timeout 180 videoextration.api:app"]
