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
        if gap >= 0.6:
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
    return prev_ended or gap >= 0.6

HOOK_WORDS = {"what", "why", "how", "look", "wait", "bro", "crazy", "insane", "never", "stop", "omg", "right", "yo", "listen"}

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

    # 1. Extract auto-subtitles via yt-dlp using cookies, Deno runtime, and remote EJS components
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

    # 2. Viral Hook & Engagement Scoring Engine
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
        start_bonus = 1.4 if has_hook else 0.9

        char_count = len(start_utt["text"])
        full_window_text = start_utt["text"]

        for j in range(i + 1, len(utterances)):
            utt_j = utterances[j]
            char_count += len(utt_j["text"])
            full_window_text += " " + utt_j["text"]
            win_end = utt_j["end"]
            length = win_end - win_start

            if length < 15:
                continue
            if length > 178:
                break

            if not is_clean_ending(utterances, j):
                continue

            density = char_count / length
            punctuation_bonus = 1.0 + (min(full_window_text.count("?") + full_window_text.count("!"), 6) * 0.04)
            position_ratio = win_start / total_video_duration
            pos_multiplier = 0.4 if (position_ratio < 0.03 or position_ratio > 0.97) else 1.0

            viral_score = density * start_bonus * punctuation_bonus * pos_multiplier

            if viral_score > highest_score:
                highest_score = viral_score
                best_start = win_start
                best_end = win_end

    if best_start is None or best_end is None:
        best_start = next((u["start"] for u in utterances if u["start"] >= 15), utterances[0]["start"])
        valid_ends = [u["end"] for u in utterances if 15 <= (u["end"] - best_start) <= 178]
        best_end = valid_ends[len(valid_ends) // 2] if valid_ends else best_start + 30

    start_time = max(0.0, round(best_start - 0.2, 2))
    end_time = round(min(best_start + 180.0, best_end + 0.4), 2)

    # 3. Generate precise SRT subtitle file for this exact clip window
    clip_utterances = [
        u for u in utterances 
        if u["end"] > best_start and u["start"] < best_end
    ]

    def fmt_time(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, u in enumerate(clip_utterances, 1):
            s_start = max(0.0, u["start"] - best_start)
            s_end = u["end"] - best_start
            if s_end <= s_start:
                s_end = s_start + 0.5
            f.write(f"{idx}\n{fmt_time(s_start)} --> {fmt_time(s_end)}\n{u['text'].strip()}\n\n")

    # 4. Download exact dynamically sized window via yt-dlp using cookies
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

    # 5. Render vertical clip + Burn-in Auto Subtitles via FFmpeg
    # Styles: Bright yellow text, bold black outline, centered near the bottom
    subtitles_filter = f"subtitles={srt_path}:force_style='FontSize=28,PrimaryColour=&H00FFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,Alignment=2,MarginV=150'"
    video_filter = f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,{subtitles_filter}"

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
        raise HTTPException(status_code=500, detail=f"FFmpeg subtitle burning failed: {e.stderr.decode('utf-8', errors='ignore')}")

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