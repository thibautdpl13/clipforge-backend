import os
import re
import glob
import json
import subprocess
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ClipRequest(BaseModel):
    url: str

@app.get("/")
def health_check():
    return {"status": "running", "service": "ClipForge AI Backend"}

def is_clean_ending(utterances, idx):
    """Checks if utterance at idx ends a complete sentence or has a natural speech pause after it."""
    text = utterances[idx]["text"].strip()
    ends_with_punct = text.endswith((".", "!", "?"))
    
    has_pause = False
    if idx + 1 < len(utterances):
        gap = utterances[idx + 1]["start"] - utterances[idx]["end"]
        if gap >= 0.6:
            has_pause = True
    else:
        has_pause = True

    return ends_with_punct or has_pause

def is_clean_start(utterances, idx):
    """Checks if utterance at idx starts after a completed sentence or natural pause."""
    if idx == 0:
        return True
    prev_text = utterances[idx - 1]["text"].strip()
    prev_ended = prev_text.endswith((".", "!", "?"))
    gap = utterances[idx]["start"] - utterances[idx - 1]["end"]
    return prev_ended or gap >= 0.6

@app.post("/generate-auto-clip")
def generate_auto_clip(request: ClipRequest, background_tasks: BackgroundTasks):
    raw_video_path = "/tmp/raw_download.mp4"
    output_path = "/tmp/clip_output.mp4"

    for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    # 1. Extract auto-subtitles via yt-dlp + local Rust PO Token server (bypasses cloud IP block)
    sub_cmd = [
        "yt-dlp",
        "--write-auto-subs",
        "--sub-format", "json3",
        "--skip-download",
        "--extractor-args", "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
        "--output", "/tmp/sub",
        request.url
    ]

    try:
        subprocess.run(sub_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"Transcript extraction failed: {e.stderr.decode('utf-8', errors='ignore')}")

    sub_files = glob.glob("/tmp/sub.*.json3")
    if not sub_files:
        raise HTTPException(status_code=400, detail="Transcript analysis failed: No auto-captions available for this video.")

    try:
        with open(sub_files[0], "r", encoding="utf-8") as f:
            sub_data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse transcript: {str(e)}")

    events = sub_data.get("events", [])
    utterances = []
    for ev in events:
        start_ms = ev.get("tStartMs")
        duration_ms = ev.get("dDurationMs", 0)
        segs = ev.get("segs", [])
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if start_ms is not None and text and text != "\n":
            utterances.append({
                "start": start_ms / 1000.0,
                "end": (start_ms + duration_ms) / 1000.0,
                "text": text
            })

    if not utterances:
        raise HTTPException(status_code=400, detail="Transcript analysis failed: Subtitles are empty.")

    # 2. Dynamic AI Window Scoring (15s to 180s, density-based, strict sentence boundaries)
    best_start = None
    best_end = None
    highest_score = -1.0

    for i, start_utt in enumerate(utterances):
        win_start = start_utt["start"]

        if win_start < 10:
            continue

        start_bonus = 1.15 if is_clean_start(utterances, i) else 0.85
        char_count = len(start_utt["text"])

        for j in range(i + 1, len(utterances)):
            char_count += len(utterances[j]["text"])
            win_end = utterances[j]["end"]
            length = win_end - win_start

            if length < 15:
                continue
            if length > 178:
                break

            if not is_clean_ending(utterances, j):
                continue

            density = char_count / length
            score = density * start_bonus

            if score > highest_score:
                highest_score = score
                best_start = win_start
                best_end = win_end

    if best_start is None or best_end is None:
        best_start = next((u["start"] for u in utterances if u["start"] >= 10), utterances[0]["start"])
        valid_ends = [u["end"] for u in utterances if 15 <= (u["end"] - best_start) <= 178]
        best_end = valid_ends[len(valid_ends) // 2] if valid_ends else best_start + 30

    start_time = max(0.0, round(best_start - 0.2, 2))
    end_time = round(min(best_start + 180.0, best_end + 0.4), 2)

    # 3. Download exact dynamically sized window via yt-dlp + PO token server
    section_range = f"*{start_time}-{end_time}"
    yt_dlp_cmd = [
        "yt-dlp",
        "--format", "best[ext=mp4]/best",
        "--extractor-args", "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
        "--download-sections", section_range,
        "--output", raw_video_path,
        request.url
    ]

    try:
        subprocess.run(yt_dlp_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"YouTube download failed: {e.stderr.decode('utf-8', errors='ignore')}")

    if not os.path.exists(raw_video_path):
        raise HTTPException(status_code=500, detail="Failed to download video source.")

    # 4. Render vertical clip via FFmpeg: Solid black letterboxing
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", raw_video_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-threads", "1",
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
        for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")