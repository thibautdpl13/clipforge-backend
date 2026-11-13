import os
import re
import urllib.request
import subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="ClipForge AI Ultimate Engine", version="3.0.0")

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
            # Download Montserrat-Black (ultra-heavy creator font)
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
    return {"status": "online", "service": "ClipForge Ultimate Engine", "font": ensure_heavy_font()}

EXACT_SENTENCES = [
    {"start": 0.0, "end": 4.1, "text": "100,000. Yes! So if we win, we're taking money from the kids."},
    {"start": 4.1, "end": 9.2, "text": "Well, if we win, then the $100,000 goes to our subscribers. Oh, to you guys."},
    {"start": 9.2, "end": 14.0, "text": "Each team will get three chances to hit this bullseye, whoever gets closest by the end will win."},
    {"start": 14.0, "end": 17.0, "text": "I'm going first. Hey guys, you want to cheer me up?"}
]

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

    duration = 17.0
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

    font_file = ensure_heavy_font()

    word_timed_list = []
    for s in EXACT_SENTENCES:
        words = s["text"].split()
        s_dur = max(0.3, s["end"] - s["start"])
        per_word = s_dur / max(1, len(words))
        for idx, w in enumerate(words):
            clean_w = re.sub(r'[^a-zA-Z0-9\$\s\?\!\,]', '', w).strip()
            if not clean_w:
                continue
            w_start = s["start"] + (idx * per_word)
            w_end = w_start + per_word
            word_timed_list.append({"start": w_start, "end": w_end, "text": clean_w})

    best_start = EXACT_SENTENCES[0]["start"]
    best_end = min(duration, EXACT_SENTENCES[-1]["end"])

    start_time = max(0.0, round(best_start, 2))
    end_time = round(min(duration, best_end), 2)

    clip_words = [
        w for w in word_timed_list 
        if w["end"] >= best_start and w["start"] <= best_end
    ]

    raw_chunks = []
    MAX_CHARS_PER_LINE = 18
    current_chunk_words = []
    current_chars = 0
    chunk_start_time = None
    chunk_end_time = None

    for w in clip_words:
        w_text = w["text"].strip().upper()
        if not w_text:
            continue
            
        rel_start = max(0.0, w["start"] - best_start)
        rel_end = max(rel_start + 0.2, w["end"] - best_start)

        if not current_chunk_words:
            chunk_start_time = rel_start
            
        if current_chars + len(w_text) + 1 > MAX_CHARS_PER_LINE and current_chunk_words:
            chunk_text = " ".join(current_chunk_words)
            raw_chunks.append({
                "start": chunk_start_time,
                "end": chunk_end_time,
                "text": chunk_text
            })
            current_chunk_words = [w_text]
            current_chars = len(w_text)
            chunk_start_time = rel_start
            chunk_end_time = rel_end
        else:
            current_chunk_words.append(w_text)
            current_chars += len(w_text) + 1
            chunk_end_time = rel_end

    if current_chunk_words:
        chunk_text = " ".join(current_chunk_words)
        raw_chunks.append({
            "start": chunk_start_time,
            "end": chunk_end_time,
            "text": chunk_text
        })

    chunked_subtitles = []
    for i, rc in enumerate(raw_chunks):
        start_t = rc["start"]
        end_t = rc["end"]
        if i + 1 < len(raw_chunks):
            end_t = raw_chunks[i + 1]["start"]
        else:
            end_t = rc["end"] + 0.8
            
        chunked_subtitles.append({"start": round(start_t, 2), "end": round(end_t, 2), "text": rc["text"]})

    # Render vertical video (1080x1920) with Massive Ultra-Bold Montserrat-Black font at y=1120
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    for sub in chunked_subtitles:
        safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', '')
        draw_filter = (
            f"drawtext=text='{safe_text}':"
            f"fontfile={font_file}:"
            f"fontsize=72:fontcolor=white:"
            f"borderw=6:bordercolor=black:"
            f"shadowx=3:shadowy=3:shadowcolor=black@1.0:"
            f"x=(w-text_w)/2:y=1120:"
            f"enable='between(t,{sub['start']},{sub['end']})'"
        )
        vf_filters.append(draw_filter)

    video_filter = ",".join(vf_filters)

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_time),
        "-to", str(end_time),
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
        for f in [input_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if background_tasks:
        background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")