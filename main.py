import os
import re
import random
import urllib.request
import subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="ClipForge AI Lightweight Engine", version="12.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FONT_PATH = "/tmp/heavy_font.ttf"

def ensure_heavy_font():
    if not os.path.exists(FONT_PATH):
        try:
            font_url = "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat-Black.ttf"
            urllib.request.urlretrieve(font_url, FONT_PATH)
        except Exception:
            try:
                fallback_url = "https://github.com/google/fonts/raw/main/ofl/robotoblack/Roboto-Black.ttf"
                urllib.request.urlretrieve(fallback_url, FONT_PATH)
            except Exception:
                pass
    if not os.path.exists(FONT_PATH):
        return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    return FONT_PATH

@app.on_event("startup")
def startup_event():
    ensure_heavy_font()

@app.get("/")
def health_check() -> Dict[str, Any]:
    return {
        "status": "online",
        "service": "ClipForge Lightweight Engine",
        "font": ensure_heavy_font()
    }

@app.post("/upload-and-clip")
async def upload_and_clip(
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None
) -> FileResponse:
    input_video_path = f"/tmp/{file.filename}"
    output_path = f"/tmp/clip_output_{os.getpid()}.mp4"

    for f in [input_video_path, output_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    try:
        with open(input_video_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {str(e)}")

    if not os.path.exists(input_video_path) or os.path.getsize(input_video_path) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty or missing.")

    # Get exact video duration via ffprobe
    duration = 30.0
    try:
        probe_cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", input_video_path
        ]
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout.strip():
            duration = float(res.stdout.strip())
    except Exception:
        pass

    # Random start time and duration strictly between 15s and 180s (ensuring it fits within total video length)
    max_clip_len = min(180.0, duration)
    if max_clip_len < 15.0:
        clip_duration = max(5.0, duration)
        start_time = 0.0
    else:
        clip_duration = random.uniform(15.0, max_clip_len)
        max_start = max(0.0, duration - clip_duration)
        start_time = random.uniform(0.0, max_start)

    font_file = ensure_heavy_font()

    # Dynamic styling with Montserrat-Black at y=1300
    safe_text = "CLIPFORGE AI"
    draw_filter = (
        f"drawtext=text='{safe_text}':"
        f"fontfile={font_file}:"
        f"fontsize=64:fontcolor=white:"
        f"borderw=5:bordercolor=black:"
        f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
        f"x=(w-text_w)/2:y=1300"
    )

    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        draw_filter
    ]
    video_filter = ",".join(vf_filters)

    # FFmpeg command utilizing fast input seeking (-ss before -i)
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-i", input_video_path,
        "-t", str(clip_duration),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "22",
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path
    ]

    ff_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if ff_res.returncode != 0 or not os.path.exists(output_path):
        ff_err = ff_res.stderr.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=500, detail=f"FFmpeg rendering failed: {ff_err[-300:]}")

    def cleanup():
        for f in [input_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if background_tasks:
        background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")