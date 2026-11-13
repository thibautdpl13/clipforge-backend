import os
import re
import urllib.request
import subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import speech_recognition as sr

app = FastAPI(title="ClipForge AI Real Transcription Engine", version="4.0.0")

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
    return {"status": "online", "service": "ClipForge Real Transcription Engine", "font": ensure_heavy_font()}

@app.post("/upload-and-clip")
async def upload_and_clip(
    file: UploadFile = File(...), 
    background_tasks: BackgroundTasks = None
) -> FileResponse:
    input_video_path = f"/tmp/{file.filename}"
    audio_wav_path = f"/tmp/audio_{os.getpid()}.wav"
    output_path = f"/tmp/clip_output_{os.getpid()}.mp4"

    for f in [input_video_path, audio_wav_path, output_path]:
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

    # Get exact video duration
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

    clip_duration = min(duration, 40.0)

    # Extract audio track to WAV format for speech recognition
    try:
        extract_audio_cmd = [
            "ffmpeg", "-y", "-i", input_video_path,
            "-t", str(clip_duration),
            "-ar", "16000", "-ac", "1", audio_wav_path
        ]
        subprocess.run(extract_audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio extraction failed: {str(e)}")

    # Transcribe real audio using speech recognition API
    full_transcript_text = ""
    if os.path.exists(audio_wav_path):
        recognizer = sr.Recognizer()
        try:
            with sr.AudioFile(audio_wav_path) as source:
                audio_data = recognizer.record(source)
                full_transcript_text = recognizer.recognize_google(audio_data)
        except Exception:
            # Fallback if speech recognition encounters silence or network hiccup
            full_transcript_text = "CLIPFORGE AI REAL UPLOAD MODE"

    font_file = ensure_heavy_font()

    # Split transcribed text into natural chunk phrases (max ~4-5 words per chunk for fast reading)
    words = full_transcript_text.upper().split()
    if not words:
        words = ["CLIPFORGE", "AI", "RENDER"]

    chunks_data = []
    chunk_size = 5
    total_chunks = max(1, (len(words) + chunk_size - 1) // chunk_size)
    time_per_chunk = clip_duration / total_chunks

    for i in range(0, len(words), chunk_size):
        chunk_words = words[i:i+chunk_size]
        phrase = " ".join(chunk_words)
        chunk_idx = i // chunk_size
        start_t = chunk_idx * time_per_chunk
        end_t = (chunk_idx + 1) * time_per_chunk
        chunks_data.append({
            "start": round(start_t, 2),
            "end": round(end_t, 2),
            "text": phrase
        })

    # Build FFmpeg drawtext filters for Montserrat-Black font
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    for sub in chunks_data:
        safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', '')
        draw_filter = (
            f"drawtext=text='{safe_text}':"
            f"fontfile={font_file}:"
            f"fontsize=52:fontcolor=white:"
            f"borderw=5:bordercolor=black:"
            f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
            f"x=(w-text_w)/2:y=1160:"
            f"enable='between(t,{sub['start']},{sub['end']})'"
        )
        vf_filters.append(draw_filter)

    video_filter = ",".join(vf_filters)

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-t", str(clip_duration),
        "-i", input_video_path,
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-threads", "4",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path
    ]

    ff_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if ff_res.returncode != 0 or not os.path.exists(output_path):
        ff_err = ff_res.stderr.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=500, detail=f"FFmpeg rendering failed: {ff_err[-300:]}")

    def cleanup():
        for f in [input_video_path, audio_wav_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if background_tasks:
        background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")