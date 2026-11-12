FROM python:3.11-slim

# 1. Install FFmpeg, curl, and unzip
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. Download the lightweight Rust PO Token server binary (~48MB, <50MB RAM usage)
RUN curl -fL https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64 -o /usr/local/bin/bgutil-pot \
    && chmod +x /usr/local/bin/bgutil-pot

# 3. Install the matching Rust yt-dlp plugin into yt-dlp's global plugin directory
RUN mkdir -p /etc/yt-dlp-plugins \
    && curl -fL https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-ytdlp-pot-provider-rs.zip -o /tmp/pot-plugin.zip \
    && unzip -q /tmp/pot-plugin.zip -d /etc/yt-dlp-plugins \
    && rm /tmp/pot-plugin.zip

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 4. Start the Rust PO token server in the background, wait 1s, and launch FastAPI
CMD ["sh", "-c", "bgutil-pot server --host 127.0.0.1 --port 4416 & sleep 1 && uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]