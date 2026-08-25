# NFPD Appeals Bot - production image
FROM python:3.12-slim

# Non-interactive apt; predictable Python defaults; no bytecode/cache noise.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini gives us proper PID 1 semantics so SIGTERM from docker stop reaches the
# Python process and asyncio can shut down the DB pool cleanly. We install
# exactly one init strategy - do NOT also set `init: true` in compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root runtime. UID 10001 is high enough to avoid clashes with host users.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin appeals \
    && chown -R appeals:appeals /app
USER appeals

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "bot.py"]
