import os
import re
import glob
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

COOKIES_PATH = "/tmp/cookies.txt"

@app.on_event("startup")
def startup_event():
    cookie_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookie_content:
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            f.write(cookie_content)
        print("Successfully loaded YouTube cookies from environment variables.")
    else:
        print("Warning: YOUTUBE_COOKIES environment variable not found.")

class ClipRequest(BaseModel):
    url: str

@app.get("/")
def health_check():
    return {"status": "running", "service": "ClipForge AI Backend"}

def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/")
    if parsed.hostname in ("www.youtube.com", "youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith(("/embed/", "/v/")):
            return parsed.path.split("/")[2]
    # Fallback regex search
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
    return match.group(1) if match else None

HOOK_WORDS = {"what", "why", "how", "look", "wait", "bro", "crazy", "insane", "never", "stop", "omg", "right", "yo", "listen", "this"}

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

    vid = extract_video_id(request.url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL provided.")

    # 1. Fetch transcript reliably via youtube-transcript-api
    try:
        # Try fetching transcript with cookies if available
        if os.path.exists(COOKIES_PATH):
            try:
                raw_transcript = YouTubeTranscriptApi.get_transcript(vid, cookies=COOKIES_PATH)
            except Exception:
                raw_transcript = YouTubeTranscriptApi.get_transcript(vid)
        else:
            raw_transcript = YouTubeTranscriptApi.get_transcript(vid)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Transcript extraction failed: {str(e)}")

    if not raw_transcript:
        raise HTTPException(status_code=400, detail="Transcript analysis failed: Subtitles are empty.")

    # Normalize entries
    word_timed_list = []
    for entry in raw_transcript:
        start = entry.get("start", 0.0)
        duration = entry.get("duration", 2.0)
        text = entry.get("text", "").replace("-", "").strip()
        if text and text != "\n":
            word_timed_list.append({
                "start": start,
                "end": start + duration,
                "text": text
            })

    if not word_timed_list:
        raise HTTPException(status_code=400, detail="Transcript analysis failed: No valid subtitle entries.")

    # 2. Viral Hook & Continuous Window Scoring Engine
    best_start = None
    best_end = None
    highest_score = -1.0

    for i, w in enumerate(word_timed_list):
        win_start = w["start"]
        if win_start < 15:
            continue

        full_window_text = ""
        win_end = win_start
        char_count = 0

        for j in range(i, len(word_timed_list)):
            wj = word_timed_list[j]
            char_count += len(wj["text"])
            full_window_text += " " + wj["text"]
            win_end = wj["end"]
            length = win_end - win_start

            if length < 20:
                continue
            if length > 65:
                break

            density = char_count / length
            has_hook = any(hw in wj["text"].lower() for hw in HOOK_WORDS) or "?" in full_window_text
            start_bonus = 1.5 if has_hook else 0.8
            viral_score = density * start_bonus

            if viral_score > highest_score:
                highest_score = viral_score
                best_start = win_start
                best_end = win_end

    if best_start is None or best_end is None:
        best_start = max(15.0, word_timed_list[0]["start"])
        best_end = min(best_start + 45.0, word_timed_list[-1]["end"])

    start_time = max(0.0, round(best_start - 0.2, 2))
    end_time = round(min(best_start + 65.0, best_end + 0.4), 2)

    # 3. Precision Character-Based Chunking (~22 chars) relative to clip window
    clip_entries = [
        e for e in word_timed_list 
        if e["end"] > best_start and e["start"] < best_end
    ]

    raw_chunks = []
    MAX_CHARS_PER_LINE = 22
    
    current_chunk_words = []
    current_chars = 0
    chunk_start_time = None
    chunk_end_time = None

    for entry in clip_entries:
        words = entry["text"].split()
        e_start = max(0.0, entry["start"] - best_start)
        e_duration = max(0.2, (entry["end"] - entry["start"]) / max(1, len(words)))
        
        for idx, w_text in enumerate(words):
            w_text = w_text.strip().upper()
            if not w_text:
                continue
            w_start = e_start + (idx * e_duration)
            w_end = w_start + e_duration

            if not current_chunk_words:
                chunk_start_time = w_start

            if current_chars + len(w_text) + 1 > MAX_CHARS_PER_LINE and current_chunk_words:
                chunk_text = " ".join(current_chunk_words)
                raw_chunks.append({
                    "start": max(0.0, chunk_start_time),
                    "end": max(chunk_start_time + 0.2, chunk_end_time),
                    "text": chunk_text
                })
                current_chunk_words = [w_text]
                current_chars = len(w_text)
                chunk_start_time = w_start
                chunk_end_time = w_end
            else:
                current_chunk_words.append(w_text)
                current_chars += len(w_text) + 1
                chunk_end_time = w_end

    if current_chunk_words:
        chunk_text = " ".join(current_chunk_words)
        raw_chunks.append({
            "start": max(0.0, chunk_start_time),
            "end": max(chunk_start_time + 0.2, chunk_end_time),
            "text": chunk_text
        })

    # 4. Strict 0-Buffer & Non-Overlapping Timeline Enforcement
    raw_chunks.sort(key=lambda x: x["start"])

    chunked_subtitles = []
    for rc in raw_chunks:
        start_t = rc["start"]
        end_t = rc["end"]
        if end_t <= start_t:
            end_t = start_t + 0.3
        chunked_subtitles.append({
            "start": start_t,
            "end": end_t,
            "text": rc["text"]
        })

    # Force exact 0-buffer handover: Each chunk ends exactly where the next one starts
    for i in range(len(chunked_subtitles) - 1):
        next_start = chunked_subtitles[i + 1]["start"]
        if next_start > chunked_subtitles[i]["start"]:
            chunked_subtitles[i]["end"] = next_start
        else:
            chunked_subtitles[i + 1]["start"] = chunked_subtitles[i]["end"]

    for sub in chunked_subtitles:
        sub["start"] = round(sub["start"], 3)
        sub["end"] = round(sub["end"], 3)

    if chunked_subtitles:
        chunked_subtitles[-1]["end"] = round(chunked_subtitles[-1]["start"] + 1.5, 3)

    # 5. Download exact video window via yt-dlp
    section_range = f"*{start_time}-{end_time}"
    yt_dlp_cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "--format", "best[ext=mp4]/best",
        "--download-sections", section_range,
        "--output", raw_video_path,
        request.url
    ]

    if os.path.exists(COOKIES_PATH):
        yt_dlp_cmd.extend(["--cookies", COOKIES_PATH])

    try:
        subprocess.run(yt_dlp_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"YouTube download failed: {e.stderr.decode('utf-8', errors='ignore')}")

    if not os.path.exists(raw_video_path):
        raise HTTPException(status_code=500, detail="Failed to download video source.")

    # 6. Render vertical clip + Asap Black font, small shadow, zero overlap/zero buffer
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    for sub in chunked_subtitles:
        safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%")
        draw_filter = (
            f"drawtext=text='{safe_text}':"
            f"fontfile=/usr/share/fonts/truetype/asap/Asap-Black.ttf:"
            f"fontsize=55:fontcolor=white:"
            f"borderw=4:bordercolor=black:"
            f"shadowx=1:shadowy=1:shadowcolor=black@0.8:"
            f"x=(w-text_w)/2:y=1180:"
            f"enable='between(t,{sub['start']},{sub['end']})'"
        )
        vf_filters.append(draw_filter)

    video_filter = ",".join(vf_filters)

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", raw_video_path,
        "-vf", video_filter,
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