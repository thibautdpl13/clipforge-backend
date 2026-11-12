FROM python:3.11-slim

# 1. Install FFmpeg, curl, nodejs, and npm
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 2. Start FastAPI with proper uvicorn flag syntax
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]