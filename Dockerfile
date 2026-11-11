FROM python:3.10-slim

# Install system dependencies, curl, ffmpeg, and Deno runtime
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    ca-certificates \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno globally
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]