import os
import glob
import json
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

# Enable CORS for frontend communication
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

@app.post("/generate-auto-clip")
def generate_auto_clip(request: ClipRequest, background_tasks: BackgroundTasks):
    raw_video_path = "/tmp/raw_download.mp4"
    output_path = "/tmp/clip_output.mp4"

    # Cleanup any lingering temporary files
    for f in glob.glob("/tmp/sub.*") + [raw_video_path, output_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except:
                pass

    # 1. Download auto-subtitles / transcript via yt-dlp for full AI analysis
    sub_cmd = [
        "yt-dlp",
        "--write-auto-subs",
        "--sub-format", "json3",
        "--skip-download",
        "--output", "/tmp/sub",
        request.url
    ]

    if os.path.exists("cookies.txt"):
        sub_cmd.extend(["--cookies", "cookies.txt"])

    try:
        subprocess.run(sub_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"Transcript extraction failed: {e.stderr.decode('utf-8', errors='ignore')}")

    # Locate generated subtitle file
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

    # 2. AI Transcript Window Scoring (Dynamic 15s to 180s range)
    best_start = utterances[0]["start"]
    best_end = best_start + 45  
    highest_score = -1

    for i, start_utt in enumerate(utterances):
        win_start = start_utt["start"]
        
        # Avoid opening intro silence / logos
        if win_start < 10:
            continue

        for j in range(i + 1, len(utterances)):
            win_end = utterances[j]["end"]
            length = win_end - win_start

            if length < 15:
                continue
            if length > 180:  # Hard cap at 3 minutes max
                break

            window_utterances = [u for u in utterances if u["start"] >= win_start and u["end"] <= win_end]
            score = sum(len(u["text"]) for u in window_utterances)

            if score > highest_score:
                highest_score = score
                best_start = win_start
                best_end = win_end

    start_time = max(0, int(best_start))
    duration = int(min(180, max(15, best_end - best_start)))

    # 3. Download exact analyzed window via yt-dlp
    yt_dlp_cmd = [
        "yt-dlp",
        "--format", "best[ext=mp4]/best",
        "--output", raw_video_path,
    ]

    if os.path.exists("cookies.txt"):
        yt_dlp_cmd.extend(["--cookies", "cookies.txt"])

    section_range = f"*{start_time}-{start_time + duration}"
    yt_dlp_cmd.extend(["--download-sections", section_range, request.url])

    try:
        subprocess.run(yt_dlp_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"YouTube download failed: {e.stderr.decode('utf-8', errors='ignore')}")

    if not os.path.exists(raw_video_path):
        raise HTTPException(status_code=500, detail="Failed to download video source.")

    # 4. Render vertical clip via FFmpeg: Solid black letterboxing (keeps landscape 100% intact, no blur/crop)
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
                except:
                    pass

    background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="clip.mp4")