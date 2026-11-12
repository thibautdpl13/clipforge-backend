import os
import re
import subprocess
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi

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

def extract_video_id(url: str) -> str:
    """Extracts the 11-character YouTube video ID from standard, short, embed, live, or shorts URLs."""
    parsed = urlparse(url.strip())
    if parsed.hostname in ("youtu.be", "www.youtu.be"):
        vid = parsed.path.lstrip("/").split("/")[0]
        if len(vid) == 11:
            return vid
    if parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            if "v" in qs and len(qs["v"][0]) == 11:
                return qs["v"][0]
        for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
            if parsed.path.startswith(prefix):
                vid = parsed.path[len(prefix):].split("/")[0]
                if len(vid) == 11:
                    return vid
    match = re.search(r"(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})(?:[&?\/]|$)", url)
    if match:
        return match.group(1)
    raise ValueError("Invalid YouTube URL: could not extract 11-character video ID.")

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

    for f in [raw_video_path, output_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    # 1. Extract Video ID and fetch transcript directly via youtube-transcript-api (zero cookies)
    try:
        video_id = extract_video_id(request.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        try:
            transcript_obj = transcript_list.find_transcript(["en", "en-US", "en-GB"])
        except Exception:
            transcript_obj = next(iter(transcript_list))
        fetched_snippets = transcript_obj.fetch()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Transcript extraction failed: {str(e)}")

    utterances = []
    for item in fetched_snippets:
        text = getattr(item, "text", None) if not isinstance(item, dict) else item.get("text")
        start = getattr(item, "start", None) if not isinstance(item, dict) else item.get("start")
        duration = getattr(item, "duration", 0.0) if not isinstance(item, dict) else item.get("duration", 0.0)

        if text is not None and start is not None:
            clean_text = text.replace("\n", " ").strip()
            if clean_text:
                utterances.append({
                    "start": float(start),
                    "end": float(start) + float(duration),
                    "text": clean_text
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

    # 3. Download exact dynamically sized window via yt-dlp + local Rust PO Token server (zero cookies)
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
        for f in [raw_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")