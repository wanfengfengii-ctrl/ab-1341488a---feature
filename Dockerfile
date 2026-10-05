FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Standard library only: nothing to install, so the build needs no network.
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts

EXPOSE 8000

HEALTHCHECK --interval=2s --timeout=3s --retries=15 --start-period=2s \
    CMD python scripts/healthcheck.py || exit 1

CMD ["python", "-m", "app.server"]
