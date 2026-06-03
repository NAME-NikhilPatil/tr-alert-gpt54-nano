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

COPY . .

RUN mkdir -p downloads images logs output

CMD ["python", "main.py"]