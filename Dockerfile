FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    wget \
    ca-certificates \
    poppler-utils \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    fonts-liberation \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install --with-deps chromium

RUN groupadd --system appuser && useradd --system --gid appuser --home-dir /app appuser

COPY --chown=appuser:appuser . .

RUN mkdir -p downloads images logs output && chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
