# FailSafe: gated, replayable payment-recovery agent.
# Default command serves the read-only console (decision trace, report,
# policy view, replay API) on :8080. The same image runs the batch agent:
#   docker run --env-file .env failsafe python -m failsafe.agent
# Ollama is NOT in the image; point OLLAMA_HOST at one you run.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    FAILSAFE_OUT_DIR=/tmp/failsafe-pages \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# The `failsafe` package lives in src/failsafe (PYTHONPATH above).
COPY src/ src/
COPY config/ config/
COPY data/ data/
COPY logs/ logs/
COPY scripts/ scripts/
COPY examples/ examples/

RUN useradd --create-home --uid 10001 failsafe && chown -R failsafe:failsafe /app/logs
USER failsafe

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"

CMD ["python", "-m", "failsafe.console"]
