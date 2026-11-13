import os
import re
import urllib.request
import subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from faster_whisper import WhisperModel

app = FastAPI(title="ClipForge AI SkyBlue Highlight Engine", version="11.0.0")

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
        "service": "ClipForge SkyBlue Highlight Engine",
        "model": MODEL_SIZE,
        "font": ensure_heavy_font()
    }

def clean_and_format_text(text: str) -> str:
    formatted = re.sub(r'(\d)\s*,\s*(\d)', r'\1,\2', text)
    return formatted.upper()

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

    duration = 45.0
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

    clip_duration = min(duration, 45.0)

    try:
        extract_cmd = [
            "ffmpeg", "-y", "-i", input_video_path,
            "-t", str(clip_duration),
            "-ar", "16000", "-ac", "1", audio_wav_path
        ]
        subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audio extraction failed: {str(e)}")

    word_timed_list = []
    model = get_whisper_model()
    if model and os.path.exists(audio_wav_path):
        try:
            segments, _ = model.transcribe(audio_wav_path, word_timestamps=True, beam_size=1)
            for segment in segments:
                if hasattr(segment, "words") and segment.words:
                    for w in segment.words:
                        raw_w = w.word.strip()
                        clean_word = re.sub(r'[^a-zA-Z0-9\$\'\,\s\?\!]', '', raw_w).strip()
                        if clean_word:
                            formatted_word = clean_and_format_text(clean_word)
                            word_timed_list.append({
                                "start": float(w.start),
                                "end": float(w.end),
                                "text": formatted_word
                            })
        except Exception as e:
            print(f"Whisper transcription error: {e}")

    if not word_timed_list:
        word_timed_list = [
            {"start": 0.0, "end": clip_duration, "text": "CLIPFORGE AI"}
        ]

    font_file = ensure_heavy_font()

    # Group words into sentences/phrases of ~4-5 words so they display together on screen,
    # while tracking individual word timings for the sky blue box highlight effect.
    phrases = []
    current_phrase_words = []
    
    for w in word_timed_list:
        current_phrase_words.append(w)
        if len(current_phrase_words) >= 5 or w == word_timed_list[-1]:
            p_start = current_phrase_words[0]["start"]
            p_end = current_phrase_words[-1]["end"]
            phrases.append({
                "start": p_start,
                "end": max(p_end, p_start + 0.8),
                "words": list(current_phrase_words)
            })
            current_phrase_words = []

    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    # For each phrase, draw the full background sentence in white with black border,
    # then draw the currently active word with the sky blue background box highlight (#00C0FF).
    for p in phrases:
        full_phrase_text = " ".join([w["text"] for w in p["words"]])
        safe_full_text = full_phrase_text.replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', '')

        # 1. Draw base white text for the entire phrase
        base_draw = (
            f"drawtext=text='{safe_full_text}':"
            f"fontfile={font_file}:"
            f"fontsize=52:fontcolor=white:"
            f"borderw=5:bordercolor=black:"
            f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
            f"x=(w-text_w)/2:y=1160:"
            f"enable='between(t,{p['start']},{p['end']})'"
        )
        vf_filters.append(base_draw)

        # 2. Layer active sky blue box highlight over each specific word when active
        for w in p["words"]:
            safe_word = w["text"].replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', '')
            highlight_draw = (
                f"drawtext=text='{safe_word}':"
                f"fontfile={font_file}:"
                f"fontsize=52:fontcolor=white:"
                f"box=1:boxcolor=0x00C0FF@1.0:boxborderw=8:"
                f"borderw=2:bordercolor=black:"
                f"x=(w-{len(safe_full_text)}*14)/2:y=1160:" # Centered alignment approximation
                f"enable='between(t,{w['start']},{w['end']})'"
            )
            # To ensure precise centering per word, we compute relative positioning or draw dynamically
            # For robust centering in ffmpeg, let's draw the active word with box at exact computed coordinate
            pass

    # Let's refine the word highlight filter with precise word positioning using drawtext box
    # Instead of complex multi-pass, let's build clean individual word highlights:
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    # Render word by word with sky blue box when active
    for w in word_timed_list:
        safe_w = w["text"].replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', '')
        word_draw = (
            f"drawtext=text='{safe_w}':"
            f"fontfile={font_file}:"
            f"fontsize=52:fontcolor=white:"
            f"box=1:boxcolor=0x00C0FF@1.0:boxborderw=10:"
            f"borderw=3:bordercolor=black:"
            f"x=(w-text_w)/2:y=1160:"
            f"enable='between(t,{w['start']},{w['end']})'"
        )
        vf_filters.append(word_draw)

    video_filter = ",".join(vf_filters)

    ffmpeg_cmd = [
        "ffmpeg", "-y",
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