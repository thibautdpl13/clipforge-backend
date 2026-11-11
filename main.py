import os
import subprocess
import re
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import yt_dlp

app = FastAPI(title="ClipForge Backend")

COOKIES_PATH = "cookies.txt"

@app.get("/")
def health_check():
    return {"status": "ClipForge Docker Backend is running with Auto-Crop & Fast Blur!"}

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

@app.get("/generate-auto-clip")
def generate_auto_clip(url: str, start_time: float, end_time: float, output_filename: str = "output_short.mp4"):
    input_file = "downloaded_source.mp4"
    
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': input_file,
        'quiet': True,
    }
    
    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH

    try:
        section_spec = f"*{start_time}-{end_time}"
        ydl_opts['download_sections'] = [section_spec]
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

    if not os.path.exists(input_file):
        raise HTTPException(status_code=500, detail="Source video slice failed to generate.")

    # 1. Automatically detect and strip black side/top bars (pillarboxes/letterboxes)
    crop_filter = detect_crop(input_file)

    # 2. Build the filter graph: Crop first (if needed), then apply fast downscale blur & centered foreground overlay
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
        output_filename
    ]

    try:
        subprocess.run(ffmpeg_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        if os.path.exists(input_file):
            os.remove(input_file)
        raise HTTPException(status_code=500, detail=f"FFmpeg encoding failed: {e.stderr.decode('utf-8')}")

    if os.path.exists(input_file):
        os.remove(input_file)

    if not os.path.exists(output_filename):
        raise HTTPException(status_code=500, detail="Output file was not generated.")

    return FileResponse(output_filename, media_type="video/mp4", filename=output_filename)