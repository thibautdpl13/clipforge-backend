import os
import re
import glob
import subprocess
import json
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "running", "service": "ClipForge AI Backend (yt-dlp Subtitle Engine)"}

HOOK_WORDS = {"what", "why", "how", "look", "wait", "bro", "crazy", "insane", "never", "stop", "omg", "right", "yo", "listen", "this", "guess", "here"}

def extract_video_id(url: str) -> str:
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:shorts\/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url.strip()

@app.post("/upload-and-clip")
async def upload_and_clip(
    file: UploadFile = File(...), 
    url: str = Form(default=""),
    background_tasks: BackgroundTasks = None
):
    input_video_path = f"/tmp/{file.filename}"
    output_path = "/tmp/clip_output.mp4"

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

    # 1. Get video duration using ffprobe
    duration = 60.0
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

    # 2. Extract transcript using yt-dlp subtitle dump (Bypasses download blocks completely)
    word_timed_list = []
    clean_url = url.strip()
    
    if clean_url:
        try:
            # Use yt-dlp to write subtitles to JSON without downloading media
            sub_json_cmd = [
                "yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
                "--sub-format", "json3", "-o", "/tmp/ytdlp_sub", clean_url
            ]
            subprocess.run(sub_json_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            
            # Find generated json3 subtitle files in /tmp
            sub_files = glob.glob("/tmp/ytdlp_sub*.json3")
            if sub_files:
                with open(sub_files[0], "r", encoding="utf-8") as sf:
                    sub_data = json.load(sf)
                    for event in sub_data.get("events", []):
                        segs = event.get("segs", [])
                        t_start = event.get("tStartMs", 0) / 1000.0
                        d_dur = event.get("dDurationMs", 2000) / 1000.0
                        
                        full_text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
                        if not full_text:
                            continue
                            
                        words = full_text.split()
                        per_word = (d_dur / max(1, len(words)))
                        for idx, w in enumerate(words):
                            clean_w = re.sub(r'[^a-zA-Z0-9\s\?\!]', '', w).strip()
                            if clean_w:
                                w_start = t_start + (idx * per_word)
                                w_end = w_start + per_word
                                word_timed_list.append({"start": w_start, "end": w_end, "text": clean_w})
        except Exception as e:
            print(f"yt-dlp subtitle extraction error: {e}")

    # Fallback if yt-dlp subtitles couldn't be parsed
    if not word_timed_list:
        raise HTTPException(status_code=400, detail="Could not extract subtitles. Please ensure a valid YouTube URL is provided.")

    # 3. Intelligent Viral Hook Scoring Engine
    best_start = word_timed_list[0]["start"]
    best_end = word_timed_list[-1]["end"]
    highest_score = -1.0
    target_clip_length = min(35.0, duration)

    for i, w in enumerate(word_timed_list):
        win_start = w["start"]
        if win_start > duration - 15:
            break

        full_window_text = ""
        win_end = win_start
        char_count = 0

        for j in range(i, len(word_timed_list)):
            wj = word_timed_list[j]
            char_count += len(wj["text"])
            full_window_text += " " + wj["text"]
            win_end = wj["end"]
            length = win_end - win_start

            if length < 15:
                continue
            if length > 45:
                break

            density = char_count / length
            has_hook = any(hw in wj["text"].lower() for hw in HOOK_WORDS) or "?" in full_window_text
            hook_bonus = 2.5 if has_hook else 1.0
            
            length_penalty = abs(length - target_clip_length) * 0.02
            viral_score = (density * hook_bonus) - length_penalty

            if viral_score > highest_score:
                highest_score = viral_score
                best_start = win_start
                best_end = win_end

    start_time = max(0.0, round(best_start, 2))
    end_time = round(min(duration, best_end), 2)

    # 4. Precision Character-Based Chunking with Absolute Timestamp Offsetting
    clip_words = [
        w for w in word_timed_list 
        if w["end"] >= best_start and w["start"] <= best_end
    ]

    raw_chunks = []
    MAX_CHARS_PER_LINE = 22
    
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

    # 5. Render vertical video (1080x1920) with Asap-Black font at y=1180
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    for sub in chunked_subtitles:
        safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%")
        draw_filter = (
            f"drawtext=text='{safe_text}':"
            f"fontfile=/usr/share/fonts/truetype/asap/Asap-Black.ttf:"
            f"fontsize=58:fontcolor=white:"
            f"borderw=4:bordercolor=black:"
            f"shadowx=1:shadowy=1:shadowcolor=black@0.9:"
            f"x=(w-text_w)/2:y=1180:"
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
        "-crf", "28",
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "128k",
        output_path
    ]

    ff_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if ff_res.returncode != 0 or not os.path.exists(output_path):
        ff_err = ff_res.stderr.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=500, detail=f"FFmpeg rendering failed: {ff_err[-300:]}")

    def cleanup():
        for f in [input_video_path, output_path, *glob.glob("/tmp/ytdlp_sub*")]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if background_tasks:
        background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")