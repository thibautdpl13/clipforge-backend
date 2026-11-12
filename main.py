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

def is_clean_ending(utterances, idx):
    text = utterances[idx]["text"].strip()
    ends_with_punct = text.endswith((".", "!", "?"))
    
    has_pause = False
    if idx + 1 < len(utterances):
        gap = utterances[idx + 1]["start"] - utterances[idx]["end"]
        if gap >= 0.5:
            has_pause = True
    else:
        has_pause = True

    return ends_with_punct or has_pause

def is_clean_start(utterances, idx):
    if idx == 0:
        return True
    prev_text = utterances[idx - 1]["text"].strip()
    prev_ended = prev_text.endswith((".", "!", "?"))
    gap = utterances[idx]["start"] - utterances[idx - 1]["end"]
    return prev_ended or gap >= 0.5

HOOK_WORDS = {"what", "why", "how", "look", "wait", "bro", "crazy", "insane", "never", "stop", "omg", "right", "yo", "listen", "this"}

@app.post("/generate-auto-clip")
def generate_auto_clip(request: ClipRequest, background_tasks: BackgroundTasks):
    raw_video_path = "/tmp/raw_download.mp4"
    output_path = "/tmp/clip_output.mp4"
    srt_path = "/tmp/clip_subs.srt"

    for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path, srt_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    # 1. Extract auto-subtitles via yt-dlp using cookies and Deno
    sub_cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "--write-auto-subs",
        "--sub-format", "json3",
        "--skip-download",
        "--output", "/tmp/sub",
        request.url
    ]

    if os.path.exists(COOKIES_PATH):
        sub_cmd.extend(["--cookies", COOKIES_PATH])

    try:
        subprocess.run(sub_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"Transcript extraction failed: {e.stderr.decode('utf-8', errors='ignore')}")

    sub_files = glob.glob("/tmp/sub.*.json3")
    if not sub_files:
        raise HTTPException(status_code=400, detail="Metadata analysis failed: No auto-captions available for this video.")

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

    total_video_duration = utterances[-1]["end"] if utterances else 300.0

    # 2. Viral Hook & Continuous Window Scoring Engine
    best_start = None
    best_end = None
    highest_score = -1.0

    for i, start_utt in enumerate(utterances):
        win_start = start_utt["start"]

        if win_start < 15:
            continue

        if not is_clean_start(utterances, i):
            continue

        first_text_lower = start_utt["text"].lower()
        has_hook = any(hw in first_text_lower for hw in HOOK_WORDS) or "?" in first_text_lower
        start_bonus = 1.5 if has_hook else 0.8

        char_count = len(start_utt["text"])
        full_window_text = start_utt["text"]

        for j in range(i + 1, len(utterances)):
            utt_j = utterances[j]
            if utt_j["start"] - utterances[j-1]["end"] > 3.0:
                break

            char_count += len(utt_j["text"])
            full_window_text += " " + utt_j["text"]
            win_end = utt_j["end"]
            length = win_end - win_start

            if length < 20:
                continue
            if length > 65:
                break

            if not is_clean_ending(utterances, j):
                continue

            density = char_count / length
            punctuation_bonus = 1.0 + (min(full_window_text.count("?") + full_window_text.count("!"), 6) * 0.05)
            position_ratio = win_start / total_video_duration
            pos_multiplier = 0.3 if (position_ratio < 0.02 or position_ratio > 0.98) else 1.0

            viral_score = density * start_bonus * punctuation_bonus * pos_multiplier

            if viral_score > highest_score:
                highest_score = viral_score
                best_start = win_start
                best_end = win_end

    if best_start is None or best_end is None:
        best_start = next((u["start"] for u in utterances if u["start"] >= 15), utterances[0]["start"])
        best_end = min(best_start + 45.0, utterances[-1]["end"])

    start_time = max(0.0, round(best_start - 0.2, 2))
    end_time = round(min(best_start + 65.0, best_end + 0.4), 2)

    # 3. Precision Subtitle Chunking with strict spacing and non-overlapping bounds
    clip_utterances = [
        u for u in utterances 
        if u["end"] > best_start and u["start"] < best_end
    ]

    raw_chunks = []
    for u in clip_utterances:
        # Split individual words and clean them
        words = u["text"].strip().upper().split()
        if not words:
            continue
        u_duration = max(0.3, u["end"] - u["start"])
        word_duration = u_duration / len(words)

        chunk_size = 3
        for idx_w in range(0, len(words), chunk_size):
            chunk_words = words[idx_w:idx_w + chunk_size]
            chunk_text = " ".join(chunk_words)  # Ensures proper spacing between words!
            c_start = u["start"] + (idx_w * word_duration) - best_start
            c_end = c_start + (len(chunk_words) * word_duration)
            raw_chunks.append({"start": max(0.0, c_start), "end": max(c_start + 0.25, c_end), "text": chunk_text})

    chunked_subtitles = []
    for i, rc in enumerate(raw_chunks):
        start_t = rc["start"]
        end_t = rc["end"]
        # Force end time to not overlap with the next chunk's start time
        if i + 1 < len(raw_chunks):
            next_start = raw_chunks[i + 1]["start"]
            if end_t > next_start:
                end_t = next_start
        if end_t <= start_t:
            end_t = start_t + 0.25
        chunked_subtitles.append({"start": round(start_t, 2), "end": round(end_t, 2), "text": rc["text"]})

    # 4. Download exact window via yt-dlp
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

    # 5. Render vertical clip + cleanly spaced drawtext subtitles
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    for sub in chunked_subtitles:
        safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%")
        draw_filter = (
            f"drawtext=text='{safe_text}':"
            f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"fontsize=55:fontcolor=white:"
            f"borderw=4:bordercolor=black:"
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
        for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path, srt_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")