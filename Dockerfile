FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS age-tool

ADD --checksum=sha256:bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377 \
    https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-linux-amd64.tar.gz \
    /tmp/age.tar.gz
RUN tar -xzf /tmp/age.tar.gz -C /tmp \
    && install -o root -g root -m 0755 /tmp/age/age /usr/local/bin/age

FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=3000 \
    DATA_DIR=/app/data

WORKDIR /app

COPY --from=age-tool /usr/local/bin/age /usr/local/bin/age

RUN addgroup --gid 10001 bibitasks \
    && adduser --uid 10001 --gid 10001 --disabled-password --gecos "" bibitasks \
    && install -d -o bibitasks -g bibitasks -m 0700 /app/data \
       /run/bibitasks-backup /var/lib/bibitasks-monitor

COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --requirement requirements.lock

COPY main.py index.html privacy.html logo.jpg ./
COPY scripts/backup.py scripts/backup_scheduler.py scripts/deployment_guard.py scripts/pilot_monitor.py \
    scripts/pilot_monitor_launcher.py scripts/restore.py scripts/secret_recovery_evidence.py \
    scripts/recovery_key_canary.py \
    scripts/bootstrap_production_env.py scripts/telegram_inventory.py \
    scripts/telegram_join_request_link.py scripts/telegram_preflight.py \
    scripts/telegram_public_surface_audit.py \
    scripts/telegram_surface_setup.py ./scripts/

USER bibitasks
EXPOSE 3000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','3000')+'/health/live', timeout=3).close()"]

CMD ["python", "main.py"]
