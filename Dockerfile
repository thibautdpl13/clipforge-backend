FROM python:3.10-slim

# Install system dependencies: Node.js (for yt-dlp n-challenge decryption), FFmpeg, and curl/git
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Expose Render's default port
EXPOSE 10000

# Start FastAPI server using Uvicorn
CMD ["uvicorn", "main.py:app", "--host", "0.0.0.0", "--port", "10000"]