# Runs the receptionist as a long-lived server. It has to be long-lived: a phone
# call is one WebSocket held open for the whole conversation, which is exactly
# what serverless platforms (Vercel, Netlify, Lambda) cannot do.
FROM python:3.11-slim

# libgomp1 is needed by onnxruntime, which runs the Silero voice-activity model
# that decides when the caller has stopped speaking.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so code edits do not invalidate the layer.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

COPY bot.py ./
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Captured leads and bookings live here. Mount a volume on it or they vanish
# with the container.
ENV DB_PATH=/data/receptionist.db
RUN mkdir -p /data

ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD curl -fsS "http://localhost:${PORT}/status" || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
