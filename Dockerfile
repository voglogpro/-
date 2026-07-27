FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=3000 \
    DATA_DIR=/app/data

WORKDIR /app

RUN addgroup --system bibitasks \
    && adduser --system --ingroup bibitasks bibitasks \
    && install -d -o bibitasks -g bibitasks -m 0700 /app/data

COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --requirement requirements.lock

COPY main.py index.html logo.jpg ./
COPY scripts/backup.py scripts/backup_scheduler.py scripts/deployment_guard.py scripts/restore.py ./scripts/

USER bibitasks
EXPOSE 3000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','3000')+'/health/live', timeout=3).close()"]

CMD ["python", "main.py"]
