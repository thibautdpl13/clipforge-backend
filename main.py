import os
import subprocess
import re
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="ClipForge Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

COOKIES_PATH = "cookies.txt"

class ClipRequest(BaseModel):
    url: str
    start_time: float = 0.0
    end_time: float = 60.0
    output_filename: str = "output_short.mp4"

@app.get("/")
def health_check():
    return {"status": "ClipForge Backend is fully operational!"}

def detect_crop(input_file: str) -> str:
    """Scans the video slice to automatically detect and strip black pillarbox or letterbox bars."""
    try:
        cmd = [
            "ffmpeg", "-i", input_file, "-vframes", "30",
            "-vf", "cropdetect=24:16:0", "-f", "null", "-"
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        matches = re.findall(r'crop=(\d+:\d+:\d+:\d+)', result.stderr)
        if matches:
            return f"crop={matches[-1]}"
    except Exception:
        pass
    return None

@app.post("/generate-auto-clip")
def generate_auto_clip(req: ClipRequest):
    input_file = "downloaded_source.mp4"
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': input_file,
        'quiet': True,
        'nocheckcertificate': True,
        'extractor_args': {'youtube': {'player_client': ['visionos', 'web_embedded', 'mweb']}},
    }
    
    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH

    try:
        section_spec = f"*{req.start_time}-{req.end_time}"
        ydl_opts['download_sections'] = [section_spec]
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([req.url])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

    if not os.path.exists(input_file):
        raise HTTPException(status_code=500, detail="Source video slice failed to generate.")

    crop_filter = detect_crop(input_file)

    if crop_filter:
        adaptive_layout_filter = (
            f"[0:v]{crop_filter}[cleaned];"
            "[cleaned]scale=270:480,boxblur=10:5,scale=1080:1920:flags=neighbor[bg];"
            "[cleaned]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        adaptive_layout_filter = (
            "[0:v]scale=270:480,boxblur=10:5,scale=1080:1920:flags=neighbor[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-filter_complex", adaptive_layout_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        req.output_filename
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        if os.path.exists(input_file):
            os.remove(input_file)
        raise HTTPException(status_code=500, detail=f"FFmpeg encoding failed: {e.stderr.decode('utf-8')}")

    if os.path.exists(input_file):
        os.remove(input_file)

    if not os.path.exists(req.output_filename):
        raise HTTPException(status_code=500, detail="Output file was not generated.")

    return FileResponse(req.output_filename, media_type="video/mp4", filename=req.output_filename)