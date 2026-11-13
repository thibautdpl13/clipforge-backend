import os
import re
import urllib.request
import subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from faster_whisper import WhisperModel

app = FastAPI(title="ClipForge AI Smart-Clip Engine", version="11.0.0")

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
        "service": "ClipForge Smart-Clip Engine",
        "model": MODEL_SIZE,
        "font": ensure_heavy_font()
    }

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

    # 1. Get total video duration via ffprobe
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

    target_clip_len = 35.0
    start_time = 0.0

    # Extract full audio WAV for Whisper and analysis
    try:
        extract_cmd = [
            "ffmpeg", "-y", "-i", input_video_path,
            "-ar", "16000", "-ac", "1", audio_wav_path
        ]
        subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio extraction failed: {str(e)}")

    # 2. Transcribe full video to get word timestamps across the entire timeline
    word_timed_list = []
    model = get_whisper_model()
    if model and os.path.exists(audio_wav_path):
        try:
            segments, _ = model.transcribe(audio_wav_path, word_timestamps=True, beam_size=1)
            for segment in segments:
                if hasattr(segment, "words") and segment.words:
                    for w in segment.words:
                        raw_w = w.word.strip()
                        clean_w = re.sub(r'[^a-zA-Z0-9\$\'\,\s\?\!]', '', raw_w).strip()
                        if clean_w:
                            word_timed_list.append({
                                "start": float(w.start),
                                "end": float(w.end),
                                "text": clean_w.upper()
                            })
        except Exception as e:
            print(f"Whisper transcription error: {e}")

    # 3. Smart Hook Finder: Find the 35s window with the highest speech density / punctuation excitement
    if duration > target_clip_len and len(word_timed_list) > 5:
        best_score = -1
        # Slide a window across word timestamps in 1-second increments
        max_possible_start = max(0.0, duration - target_clip_len)
        t_cursor = 0.0
        while t_cursor <= max_possible_start:
            window_end = t_cursor + target_clip_len
            # Count words and exciting markers in this window
            window_words = [w for w in word_timed_list if t_cursor <= w["start"] < window_end]
            score = len(window_words)
            for w in window_words:
                if "!" in w["text"] or "?" in w["text"] or "$" in w["text"]:
                    score += 3  # boost high-stakes words
            
            if score > best_score:
                best_score = score
                start_time = t_cursor
            t_cursor += 2.0

    clip_duration = min(target_clip_len, duration - start_time)
    if clip_duration < 5.0:
        start_time = 0.0
        clip_duration = min(target_clip_len, duration)

    # Filter words to only those inside our chosen high-energy time window and normalize timestamps relative to clip start
    windowed_words = [w for w in word_timed_list if start_time <= w["start"] < (start_time + clip_duration)]
    for w in windowed_words:
        w["start"] = max(0.0, w["start"] - start_time)
        w["end"] = max(0.0, w["end"] - start_time)

    # Merge split number/comma tokens (e.g. "100" and ",000" -> "100,000")
    merged_list = []
    for w in windowed_words:
        if merged_list and (w["text"].startswith(",") or w["text"] == ","):
            merged_list[-1]["text"] += w["text"]
            merged_list[-1]["end"] = w["end"]
        else:
            merged_list.append(w)

    # Strip useless "A " before dollar amounts
    final_word_list = []
    i = 0
    while i < len(merged_list):
        curr = merged_list[i]
        if curr["text"] == "A" and i + 1 < len(merged_list) and merged_list[i + 1]["text"].startswith("$"):
            next_token = merged_list[i + 1]
            next_token["start"] = curr["start"]
            final_word_list.append(next_token)
            i += 2
        else:
            final_word_list.append(curr)
            i += 1

    if not final_word_list:
        final_word_list = [
            {"start": 0.0, "end": clip_duration, "text": "CLIPFORGE AI"}
        ]

    font_file = ensure_heavy_font()

    # Intelligent Word Grouping into Reader-Friendly Chunks (Max 3-4 words)
    chunked_subtitles = []
    current_chunk = []
    chunk_start = None

    for w in final_word_list:
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
            "end": round(final_word_list[-1]["end"], 2),
            "text": " ".join(current_chunk)
        })

    # Render vertical video slice with smart offset start time
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

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-t", str(clip_duration),
        "-i", input_video_path,
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "22",
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