import os
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

# Enable CORS for your GitHub Pages frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ClipRequest(BaseModel):
    url: str
    start_time: int = 0
    duration: int = 30

@app.get("/")
def health_check():
    return {"status": "running", "service": "ClipForge AI Backend"}

@app.post("/generate-auto-clip")
def generate_auto_clip(request: ClipRequest, background_tasks: BackgroundTasks):
    raw_video_path = "/tmp/raw_download.mp4"
    output_path = "/tmp/clip_output.mp4"

    # Clean up any leftover files from previous requests
    for path in [raw_video_path, output_path]:
        if os.path.exists(path):
            os.remove(path)

    # 1. Download video section via yt-dlp using cookies if available
    yt_dlp_cmd = [
        "yt-dlp",
        "--format", "best[ext=mp4]/best",
        "--output", raw_video_path,
    ]

    # Corrected flag: yt-dlp uses --cookies (plural)
    if os.path.exists("cookies.txt"):
        yt_dlp_cmd.extend(["--cookies", "cookies.txt"])

    # Download specific section to save bandwidth and disk space
    section_range = f"*{request.start_time}-{request.start_time + request.duration}"
    yt_dlp_cmd.extend(["--download-sections", section_range, request.url])

    try:
        subprocess.run(yt_dlp_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"YouTube download failed: {e.stderr.decode('utf-8', errors='ignore')}")

    if not os.path.exists(raw_video_path):
        raise HTTPException(status_code=500, detail="Failed to download video source.")

    # 2. Render vertical clip via FFmpeg with strict RAM limits for Render's 512MB container
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", raw_video_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-c:v", "libx264",
        "-preset", "ultrafast",        # Keeps CPU/RAM buffer consumption extremely low
        "-crf", "28",                  # Balanced file compression
        "-threads", "1",               # CRITICAL: Prevents multi-thread OOM crashes on Render
        "-c:a", "aac",
        "-b:a", "128k",
        output_path
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg rendering failed: {e.stderr.decode('utf-8', errors='ignore')}")

    if not os.path.exists(output_path):
        raise HTTPException(status_code=500, detail="Clip rendering completed but output file is missing.")

    def cleanup():
        for path in [raw_video_path, output_path]:
            if os.path.exists(path):
                os.remove(path)

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")