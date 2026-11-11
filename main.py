import subprocess
import json
import os
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
from urllib.parse import urlparse, parse_qs

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoURL(BaseModel):
    url: str

def extract_video_id(url: str) -> str:
    parsed_url = urlparse(url)
    if parsed_url.hostname in ['youtu.be', 'www.youtu.be']:
        return parsed_url.path[1:]
    if parsed_url.hostname in ['youtube.com', 'www.youtube.com']:
        if parsed_url.path == '/watch':
            return parse_qs(parsed_url.query).get('v', [None])[0]
        elif parsed_url.path.startswith(('/embed/', '/v/')):
            return parsed_url.path.split('/')[2]
    return None

def find_best_clip_window(transcript: list, target_duration: float = 30.0) -> float:
    """
    Scans the entire transcript to find a dense 30-second window 
    with the highest concentration of words/dialogue.
    """
    if not transcript or len(transcript) == 0:
        return 10.0
        
    total_duration = transcript[-1]["start"] + transcript[-1].get("duration", 5.0)
    if total_duration <= target_duration:
        return 0.0
        
    best_start = transcript[0]["start"]
    max_words = 0
    
    for i, entry in enumerate(transcript):
        window_start = entry["start"]
        window_end = window_start + target_duration
        
        current_words = 0
        for sub in transcript[i:]:
            if sub["start"] < window_end:
                current_words += len(sub["text"].split())
            else:
                break
                
        if current_words > max_words:
            max_words = current_words
            best_start = window_start
            
    return float(best_start)

@app.post("/process-video")
async def process_video(payload: VideoURL):
    video_url = payload.url
    
    command = [
        "yt-dlp",
        "--cookies", "cookies.txt",
        "--remote-components", "ejs:github",
        "--dump-json",
        "--extractor-args", "youtube:player_client=default,web_embedded",
        "--ignore-no-formats-error",
        video_url
    ]
    
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        video_data = json.loads(result.stdout)
        
        return {
            "success": True,
            "title": video_data.get("title"),
            "duration": video_data.get("duration"),
            "thumbnail": video_data.get("thumbnail"),
            "formats": video_data.get("formats", [])
        }
        
    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else str(e)
        raise HTTPException(
            status_code=400, 
            detail=f"Video processing failed: {error_message}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Internal server error: {str(e)}"
        )

@app.post("/generate-auto-clip")
async def generate_auto_clip(payload: VideoURL):
    job_id = str(uuid.uuid4())[:8]
    raw_video = f"raw_{job_id}.mp4"
    output_clip = f"viral_clip_{job_id}.mp4"
    
    video_id = extract_video_id(payload.url)
    if not video_id:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL format.")
    
    try:
        try:
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        except Exception:
            transcript_list = []
            
        start_time = find_best_clip_window(transcript_list, target_duration=30.0)
        end_time = start_time + 30.0
        
        time_range = f"*{start_time}-{end_time}"
        
        download_cmd = [
            "yt-dlp",
            "--cookies", "cookies.txt",
            "--remote-components", "ejs:github",
            "--extractor-args", "youtube:player_client=default,web_embedded",
            "--download-sections", time_range,
            "--format", "mp4/best",
            "--output", raw_video,
            payload.url
        ]
        
        process = subprocess.run(download_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if process.returncode != 0:
            error_output = process.stderr.strip() if process.stderr else "Unknown yt-dlp error"
            raise HTTPException(status_code=500, detail=f"yt-dlp download failed: {error_output}")
        
        if not os.path.exists(raw_video) or os.path.getsize(raw_video) == 0:
            raise HTTPException(status_code=500, detail="Downloaded segment file is missing or empty.")
            
        blurred_layout_filter = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=25:10[bg];"
            "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        
        ffmpeg_crop_cmd = [
            "ffmpeg", "-i", raw_video,
            "-filter_complex", blurred_layout_filter,
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            output_clip, "-y"
        ]
        ffmpeg_process = subprocess.run(ffmpeg_crop_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if ffmpeg_process.returncode != 0:
            if os.path.exists(raw_video):
                os.remove(raw_video)
            raise HTTPException(status_code=500, detail=f"FFmpeg processing failed: {ffmpeg_process.stderr.strip()}")
        
        if os.path.exists(raw_video):
            os.remove(raw_video)
            
        if not os.path.exists(output_clip):
            raise HTTPException(status_code=500, detail="Failed to render vertical short file.")
            
        return FileResponse(
            output_clip,
            media_type="video/mp4",
            filename="viral_short.mp4"
        )
        
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")

@app.get("/")
def read_root():
    return {"status": "ClipForge Docker Backend is running with Deno runtime!"}