FROM python:3.11-slim

# Install system dependencies: Node.js, NPM, FFmpeg, and git
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

# Set yt-dlp remote components environment variable globally
ENV YT_DLP_REMOTE_COMPONENTS="ejs:github"

# Expose Render's default port
EXPOSE 10000

# Start FastAPI server using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]