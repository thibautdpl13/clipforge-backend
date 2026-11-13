import os
import re
import glob
import subprocess
import requests
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

@app.on_event("startup")
def startup_event():
    # Ensure node symlink exists for yt-dlp EJS challenge solver
    try:
        if not os.path.exists("/usr/bin/node") and os.path.exists("/usr/bin/nodejs"):
            os.symlink("/usr/bin/nodejs", "/usr/bin/node")
    except Exception:
        pass

    try:
        subprocess.run(["pip", "install", "--upgrade", "yt-dlp", "youtube-transcript-api", "requests"], check=True)
        print("SUCCESS: Dependencies verified and upgraded.")
    except Exception as e:
        print(f"Startup notice: {e}")

class ClipRequest(BaseModel):
    url: str

@app.get("/")
def health_check():
    return {"status": "running", "service": "ClipForge AI Backend (EJS Hardened Mode)"}

HOOK_WORDS = {"what", "why", "how", "look", "wait", "bro", "crazy", "insane", "never", "stop", "omg", "right", "yo", "listen", "this"}

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

    video_id = extract_video_id(request.url)

    # 1. Fetch timed transcripts with robust fallbacks
    word_timed_list = []
    fetched_entries = None
    try:
        try:
            fetched_entries = YouTubeTranscriptApi.get_transcript(video_id)
        except Exception:
            try:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                for t in transcript_list:
                    try:
                        fetched_entries = t.fetch()
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        if fetched_entries:
            for entry in fetched_entries:
                start = entry.get('start', 0.0)
                dur = entry.get('duration', 2.0)
                text = entry.get('text', '').replace('\n', ' ')
                words = text.split()
                if not words:
                    continue
                per_word = dur / len(words)
                for idx, w in enumerate(words):
                    clean_w = re.sub(r'[^a-zA-Z0-9\s\?\!]', '', w).strip()
                    if clean_w:
                        w_start = start + (idx * per_word)
                        w_end = w_start + per_word
                        word_timed_list.append({"start": w_start, "end": w_end, "text": clean_w})
    except Exception as e:
        print(f"Transcript fetch notice: {str(e)}")

    if not word_timed_list:
        word_timed_list = [
            {"start": float(i * 3), "end": float((i + 1) * 3), "text": f"CLIPFORGE AI SEGMENT {i+1}"}
            for i in range(15)
        ]

    # 2. Viral Hook & Continuous Window Scoring Engine
    best_start = None
    best_end = None
    highest_score = -1.0

    for i, w in enumerate(word_timed_list):
        win_start = w["start"]
        if win_start < 5:
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

            if length < 15:
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
        best_start = max(0.0, word_timed_list[0]["start"])
        best_end = min(best_start + 45.0, word_timed_list[-1]["end"])

    start_time = max(0.0, round(best_start - 0.2, 2))
    end_time = round(min(best_start + 65.0, best_end + 0.4), 2)

    # 3. Precision Character-Based Chunking (Max 22 chars per line)
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

    # 4. Resolve video stream via Cobalt API with proper headers
    media_url = None
    try:
        api_payload = {'url': request.url, 'vCodec': 'h264', 'vQuality': '720'}
        api_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        r_api = requests.post('https://api.cobalt.tools/api/json', json=api_payload, headers=api_headers, timeout=12)
        if r_api.status_code == 200:
            data = r_api.json()
            media_url = data.get('url') or data.get('picker', [{}])[0].get('url')
    except Exception as e:
        print(f"Bridge resolver notice: {e}")

    # Fallback to yt-dlp with explicit node js_runtimes configuration if bridge misses
    if not media_url:
        try:
            import yt_dlp
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'outtmpl': raw_video_path,
                'quiet': True,
                'js_runtimes': {'node': '/usr/bin/node'},
                'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}}
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([request.url])
        except Exception as ex:
            raise HTTPException(status_code=502, detail=f"All download routes blocked: {str(ex)}")

    if media_url:
        try:
            with requests.get(media_url, stream=True, timeout=25) as r_stream:
                r_stream.raise_for_status()
                with open(raw_video_path, 'wb') as f_out:
                    for chunk in r_stream.iter_content(chunk_size=8192):
                        if chunk:
                            f_out.write(chunk)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to pull media stream from bridge: {str(e)}")

    if not os.path.exists(raw_video_path) or os.path.getsize(raw_video_path) == 0:
        raise HTTPException(status_code=500, detail="Downloaded video file is missing or empty.")

    # 5. Render vertical video (1080x1920) with Asap ExtraBold subtitles at y=1180
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
        "-ss", str(start_time),
        "-to", str(end_time),
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
        for f in [raw_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")