import os
import re
import glob
import subprocess
import requests
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
    return {"status": "running", "service": "ClipForge AI Backend (Multi-Mirror Mode)"}

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

def fetch_media_data(video_id: str):
    """Tries multiple public Invidious and Piped API instances to fetch video data and stream URLs."""
    invidious_instances = [
        "https://invidious.privacydev.net",
        "https://inv.nadeko.net",
        "https://invidious.perennialte.ch",
        "https://vid.priv.au",
        "https://invidious.nerdvpn.de",
        "https://yt.artemislabs.org"
    ]

    piped_instances = [
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.adminforge.de",
        "https://pipedapi.ducks.party"
    ]

    # 1. Try Invidious instances
    for instance in invidious_instances:
        try:
            resp = requests.get(f"{instance}/api/v1/videos/{video_id}", timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                format_streams = data.get("formatStreams", [])
                video_url = None
                for stream in format_streams:
                    if "mp4" in stream.get("type", "").lower():
                        video_url = stream.get("url")
                        break
                if not video_url and format_streams:
                    video_url = format_streams[0].get("url")
                
                if video_url:
                    if video_url.startswith("/"):
                        video_url = instance + video_url
                    return data.get("captions", []), video_url
        except Exception:
            continue

    # 2. Try Piped API instances as backup layer
    for instance in piped_instances:
        try:
            resp = requests.get(f"{instance}/streams/{video_id}", timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                video_streams = data.get("videoStreams", [])
                video_url = None
                for stream in video_streams:
                    if stream.get("url") and "mp4" in stream.get("mimeType", "").lower():
                        video_url = stream.get("url")
                        break
                if not video_url and video_streams:
                    video_url = video_streams[0].get("url")

                subtitles = []
                for sub in data.get("subtitles", []):
                    subtitles.append({
                        "languageCode": sub.get("code", "en"),
                        "url": sub.get("url")
                    })

                if video_url:
                    return subtitles, video_url
        except Exception:
            continue

    raise HTTPException(status_code=502, detail="All public Invidious and Piped mirror instances failed to respond.")

def parse_vtt_file(vtt_path):
    word_timed_list = []
    if not os.path.exists(vtt_path):
        return word_timed_list
        
    with open(vtt_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    def time_to_seconds(time_str):
        parts = time_str.strip().split(':')
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s.replace(',', '.'))
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s.replace(',', '.'))
        return 0.0

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            parts = line.split("-->")
            start_t = time_to_seconds(parts[0])
            end_t = time_to_seconds(parts.split()[2])
            
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                clean_line = re.sub(r'<[^>]*>', '', lines[i]).strip()
                if clean_line:
                    text_lines.append(clean_line)
                i += 1
            
            full_text = " ".join(text_lines).replace("-", "").strip()
            if full_text:
                words = full_text.split()
                duration = max(0.3, end_t - start_t)
                time_per_word = duration / max(1, len(words))
                for w_idx, word in enumerate(words):
                    clean_word = re.sub(r'[^a-zA-Z0-9\s\?\!]', '', word).strip()
                    if clean_word:
                        w_start = start_t + (w_idx * time_per_word)
                        w_end = w_start + time_per_word
                        word_timed_list.append({"start": w_start, "end": w_end, "text": clean_word})
        else:
            i += 1
            
    return word_timed_list

@app.post("/generate-auto-clip")
def generate_auto_clip(request: ClipRequest, background_tasks: BackgroundTasks):
    raw_video_path = "/tmp/raw_download.mp4"
    output_path = "/tmp/clip_output.mp4"
    sub_vtt_path = "/tmp/sub.vtt"

    for f in [raw_video_path, output_path, sub_vtt_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    video_id = extract_video_id(request.url)
    captions, video_stream_url = fetch_media_data(video_id)

    # 1. Download Subtitles (.vtt) if available
    sub_downloaded = False
    for cap in captions:
        if "en" in cap.get("languageCode", "").lower() or not sub_downloaded:
            cap_url = cap.get("url")
            if cap_url:
                try:
                    sub_resp = requests.get(cap_url, timeout=8)
                    if sub_resp.status_code == 200:
                        with open(sub_vtt_path, "w", encoding="utf-8") as sf:
                            sf.write(sub_resp.text)
                        sub_downloaded = True
                        if "en" in cap.get("languageCode", "").lower():
                            break
                except Exception:
                    pass

    word_timed_list = parse_vtt_file(sub_vtt_path) if sub_downloaded else []

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

    # 3. Precision Character-Based Chunking
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

    # 4. Download raw video file via streaming
    try:
        with requests.get(video_stream_url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(raw_video_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stream raw video: {str(e)}")

    if not os.path.exists(raw_video_path) or os.path.getsize(raw_video_path) == 0:
        raise HTTPException(status_code=500, detail="Downloaded raw video file is empty.")

    # 5. Render vertical clip (1080x1920) with Asap Extra Bold subtitles using FFmpeg
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
        for f in [raw_video_path, output_path, sub_vtt_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")