FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Install deps first for better layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy source
COPY src /app/src
COPY rules.yaml /app/rules.yaml

# Expose is informational; platform routes to $PORT
EXPOSE 8000

# Run FastAPI (port is injected by hosting platform)
CMD ["sh", "-lc", "uvicorn ai_trading_coach.server:app --host 0.0.0.0 --port ${PORT:-8000}"]


