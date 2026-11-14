import os
import re
import random
import uuid
import urllib.request
import subprocess
import threading
from typing import Dict, Any
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from faster_whisper import WhisperModel

app = FastAPI(title="ClipForge AI Asynchronous Engine", version="11.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FONT_PATH = "/tmp/heavy_font.ttf"
MODEL_SIZE = "tiny"
whisper_model = None

# In-memory job status store for the free tier
job_statuses = {}

def get_whisper_model():
    global whisper_model
    if whisper_model is None:
        try:
            whisper_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        except Exception as e:
            print(f"Failed to load Whisper model: {e}")
    return whisper_model

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
    get_whisper_model()

@app.get("/")
def health_check() -> Dict[str, Any]:
    return {
        "status": "online",
        "service": "ClipForge Asynchronous Engine",
        "model": MODEL_SIZE,
        "font": ensure_heavy_font()
    }

def process_video_task(task_id: str, input_video_path: str, audio_wav_path: str, output_path: str):
    try:
        job_statuses[task_id] = {"status": "processing", "progress": "Probing video duration..."}
        
        duration = 35.0
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

        job_statuses[task_id]["progress"] = "Extracting audio..."
        extract_cmd = [
            "ffmpeg", "-y", "-i", input_video_path,
            "-ar", "16000", "-ac", "1", audio_wav_path
        ]
        subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        job_statuses[task_id]["progress"] = "Transcribing audio..."
        raw_word_list = []
        segment_starts = []
        model = get_whisper_model()
        
        if model and os.path.exists(audio_wav_path):
            segments, _ = model.transcribe(audio_wav_path, word_timestamps=True, beam_size=1)
            for segment in segments:
                seg_start = float(segment.start)
                if seg_start + 15.0 <= duration:
                    segment_starts.append(seg_start)

                if hasattr(segment, "words") and segment.words:
                    for w in segment.words:
                        raw_w = w.word.strip()
                        clean_w = re.sub(r'[^a-zA-Z0-9\$\'\,\s\?\!]', '', raw_w).strip()
                        if clean_w:
                            raw_word_list.append({
                                "start": float(w.start),
                                "end": float(w.end),
                                "text": clean_w.upper()
                            })

        start_time = 0.0
        if segment_starts:
            start_time = random.choice(segment_starts)

        remaining_time = duration - start_time
        if remaining_time < 15.0:
            start_time = 0.0
            remaining_time = duration
        
        clip_duration = min(remaining_time, 45.0)

        windowed_words = [w for w in raw_word_list if start_time <= w["start"] < (start_time + clip_duration)]
        for w in windowed_words:
            w["start"] = max(0.0, w["start"] - start_time)
            w["end"] = max(0.0, w["end"] - start_time)

        merged_list = []
        for w in windowed_words:
            if merged_list and (w["text"].startswith(",") or w["text"] == ","):
                merged_list[-1]["text"] += w["text"]
                merged_list[-1]["end"] = w["end"]
            else:
                merged_list.append(w)

        word_timed_list = []
        i = 0
        while i < len(merged_list):
            curr = merged_list[i]
            if curr["text"] == "A" and i + 1 < len(merged_list) and merged_list[i + 1]["text"].startswith("$"):
                next_token = merged_list[i + 1]
                next_token["start"] = curr["start"]
                word_timed_list.append(next_token)
                i += 2
            else:
                word_timed_list.append(curr)
                i += 1

        if not word_timed_list:
            word_timed_list = [
                {"start": 0.0, "end": clip_duration, "text": "CLIPFORGE AI"}
            ]

        font_file = ensure_heavy_font()

        chunked_subtitles = []
        current_chunk = []
        chunk_start = None

        for w in word_timed_list:
            if not current_chunk:
                chunk_start = w["start"]
            current_chunk.append(w["text"])
            
            if len(current_chunk) >= 3 or len(" ".join(current_chunk)) > 16:
                chunked_subtitles.append({
                    "start": round(chunk_start, 2),
                    "end": round(w["end"], 2),
                    "text": " ".join(current_chunk)
                })
                current_chunk = []

        if current_chunk:
            chunked_subtitles.append({
                "start": round(chunk_start, 2),
                "end": round(word_timed_list[-1]["end"], 2),
                "text": " ".join(current_chunk)
            })

        vf_filters = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
        ]

        for sub in chunked_subtitles:
            safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', '')
            draw_filter = (
                f"drawtext=text='{safe_text}':"
                f"fontfile={font_file}:"
                f"fontsize=52:fontcolor=white:"
                f"borderw=5:bordercolor=black:"
                f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
                f"x=(w-text_w)/2:y=1300:"
                f"enable='between(t,{sub['start']},{sub['end']})'"
            )
            vf_filters.append(draw_filter)

        video_filter = ",".join(vf_filters)

        job_statuses[task_id]["progress"] = "Rendering vertical video with FFmpeg..."
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-t", str(clip_duration),
            "-i", input_video_path,
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-threads", "4",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]

        ff_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ff_res.returncode != 0 or not os.path.exists(output_path):
            raise Exception("FFmpeg rendering process failed.")

        job_statuses[task_id] = {"status": "completed", "output_path": output_path}

    except Exception as e:
        job_statuses[task_id] = {"status": "failed", "error": str(e)}
    finally:
        if os.path.exists(input_video_path):
            try:
                os.remove(input_video_path)
            except Exception:
                pass
        if os.path.exists(audio_wav_path):
            try:
                os.remove(audio_wav_path)
            except Exception:
                pass

@app.post("/upload-and-clip")
async def upload_and_clip(file: UploadFile = File(...)):
    task_id = str(uuid.uuid4())
    input_video_path = f"/tmp/{task_id}_{file.filename}"
    audio_wav_path = f"/tmp/audio_{task_id}.wav"
    output_path = f"/tmp/clip_output_{task_id}.mp4"

    try:
        with open(input_video_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {str(e)}")

    if not os.path.exists(input_video_path) or os.path.getsize(input_video_path) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty or missing.")

    job_statuses[task_id] = {"status": "queued"}

    # Start processing in a background thread so the HTTP request returns instantly (0 timeout risk)
    threading.Thread(
        target=process_video_task,
        args=(task_id, input_video_path, audio_wav_path, output_path),
        daemon=True
    ).start()

    return JSONResponse({"task_id": task_id, "status": "queued"})

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in job_statuses:
        raise HTTPException(status_code=404, detail="Task not found.")
    return job_statuses[task_id]

@app.get("/download/{task_id}")
async def download_result(task_id: str, background_tasks: BackgroundTasks = None):
    if task_id not in job_statuses or job_statuses[task_id]["status"] != "completed":
        raise HTTPException(status_code=400, detail="Clip is not ready yet.")
    
    output_path = job_statuses[task_id]["output_path"]
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Rendered file missing.")

    def cleanup():
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        if task_id in job_statuses:
            del job_statuses[task_id]

    if background_tasks:
        background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")