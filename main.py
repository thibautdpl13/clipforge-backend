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
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

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
    parsed_url = urlparse(url)
    if parsed_url.hostname in ['youtu.be', 'www.youtu.be']:
        return parsed_url.path[1:]
    if parsed_url.hostname in ['youtube.com', 'www.youtube.com', 'm.youtube.com']:
        if parsed_url.path == '/watch':
            return parse_qs(parsed_url.query).get('v', [None])[0]
        elif parsed_url.path.startswith(('/embed/', '/v/')):
            return parsed_url.path.split('/')[2]
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    if match:
        return match.group(1)
    raise HTTPException(status_code=400, detail="Invalid YouTube URL format.")

HOOK_WORDS = {"what", "why", "how", "look", "wait", "bro", "crazy", "insane", "never", "stop", "omg", "right", "yo", "listen", "this"}

@app.post("/generate-auto-clip")
def generate_auto_clip(request: ClipRequest, background_tasks: BackgroundTasks):
    video_id = extract_video_id(request.url)
    raw_video_path = "/tmp/raw_download.mp4"
    output_path = "/tmp/clip_output.mp4"

    for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    # 1. Fetch transcript using classic stable get_transcript method
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'fr', 'es', 'de', 'it', 'pt', 'ru', 'ja', 'ko'])
    except Exception as e:
        try:
            # Fallback to any available transcript language
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        except Exception as inner_e:
            raise HTTPException(status_code=400, detail=f"Transcript extraction failed: No captions available. ({str(inner_e)})")

    if not transcript_list:
        raise HTTPException(status_code=400, detail="Transcript analysis failed: Retrieved captions are empty.")

    word_timed_list = []
    for entry in transcript_list:
        start = entry.get("start", 0.0)
        duration = entry.get("duration", 2.0)
        text = entry.get("text", "").replace("\n", " ").strip()
        
        if not text:
            continue
            
        words = text.split()
        time_per_word = duration / max(1, len(words))
        for idx, word in enumerate(words):
            clean_word = re.sub(r'[^a-zA-Z0-9\s\?\!]', '', word).strip()
            if clean_word:
                w_start = start + (idx * time_per_word)
                w_end = w_start + time_per_word
                word_timed_list.append({"start": w_start, "end": w_end, "text": clean_word})

    if not word_timed_list:
        raise HTTPException(status_code=400, detail="Transcript analysis failed: Formatted word list is empty.")

    # 2. Viral Hook & Continuous Window Scoring Engine
    best_start = None
    best_end = None
    highest_score = -1.0

    for i, w in enumerate(word_timed_list):
        win_start = w["start"]
        if win_start < 10:
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
        best_start = max(10.0, word_timed_list[0]["start"])
        best_end = min(best_start + 45.0, word_timed_list[-1]["end"])

    start_time = max(0.0, round(best_start - 0.2, 2))
    end_time = round(min(best_start + 65.0, best_end + 0.4), 2)

    # 3. Precision Character-Based Chunking with Strict Contiguous Handover (Zero Overlap, Zero Gaps)
    clip_words = [
        w for w in word_timed_list 
        if w["end"] > best_start and w["start"] < best_end
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
            
        if not current_chunk_words:
            chunk_start_time = w["start"] - best_start
            
        if current_chars + len(w_text) + 1 > MAX_CHARS_PER_LINE and current_chunk_words:
            chunk_text = " ".join(current_chunk_words)
            raw_chunks.append({
                "start": max(0.0, chunk_start_time),
                "end": max(chunk_start_time + 0.2, chunk_end_time),
                "text": chunk_text
            })
            current_chunk_words = [w_text]
            current_chars = len(w_text)
            chunk_start_time = w["start"] - best_start
            chunk_end_time = w["end"] - best_start
        else:
            current_chunk_words.append(w_text)
            current_chars += len(w_text) + 1
            chunk_end_time = w["end"] - best_start

    if current_chunk_words:
        chunk_text = " ".join(current_chunk_words)
        raw_chunks.append({
            "start": max(0.0, chunk_start_time),
            "end": max(chunk_start_time + 0.2, chunk_end_time),
            "text": chunk_text
        })

    chunked_subtitles = []
    for i, rc in enumerate(raw_chunks):
        start_t = rc["start"]
        end_t = rc["end"]
        if i + 1 < len(raw_chunks):
            next_start = raw_chunks[i + 1]["start"]
            end_t = next_start
        else:
            end_t = rc["end"] + 1.2
            
        chunked_subtitles.append({"start": round(start_t, 2), "end": round(end_t, 2), "text": rc["text"]})

    # 4. Download exact window via yt-dlp with Render-optimized resource limits
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

    dl_res = subprocess.run(yt_dlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if dl_res.returncode != 0 or not os.path.exists(raw_video_path):
        dl_err = dl_res.stderr.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=400, detail=f"YouTube download failed: {dl_err[-300:]}")

    # 5. Render vertical clip (1080x1920) + Asap Extra Bold font (size 55, y=1180, shadowx=1:shadowy=1)
    vf_filters = [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    ]

    for sub in chunked_subtitles:
        safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%")
        draw_filter = (
            f"drawtext=text='{safe_text}':"
            f"fontfile=/usr/share/fonts/truetype/asap/Asap-ExtraBold.ttf:"
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

    ff_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if ff_res.returncode != 0 or not os.path.exists(output_path):
        ff_err = ff_res.stderr.decode('utf-8', errors='ignore')
        raise HTTPException(status_code=500, detail=f"FFmpeg rendering failed: {ff_err[-300:]}")

    def cleanup():
        for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")