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

@app.post("/process-video")
async def process_video(payload: VideoURL):
    video_url = payload.url
    
    command = [
        "yt-dlp",
        "--cookies", "cookies.txt",
        "--dump-json",
        "--extractor-args", "youtube:player_client=ios,web",
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
            transcript_list = [{"start": 0.0, "duration": 5.0, "text": "Introduction"}]
            
        start_time = float(transcript_list[0]["start"]) if transcript_list else 0.0
        end_time = start_time + 30.0
        
        time_range = f"*{start_time}-{end_time}"
        
        download_cmd = [
            "yt-dlp",
            "--cookies", "cookies.txt",
            "--extractor-args", "youtube:player_client=ios,web",
            "--download-sections", time_range,
            "--format", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "--output", raw_video,
            payload.url
        ]
        
        subprocess.run(download_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if not os.path.exists(raw_video):
            raise HTTPException(status_code=500, detail="Failed to download video segment.")
            
        ffmpeg_crop_cmd = [
            "ffmpeg", "-i", raw_video,
            "-vf", "crop=ih*(9/16):ih:(iw-ih*(9/16))/2:0,scale=1080:1920",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            output_clip, "-y"
        ]
        subprocess.run(ffmpeg_crop_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if os.path.exists(raw_video):
            os.remove(raw_video)
            
        if not os.path.exists(output_clip):
            raise HTTPException(status_code=500, detail="Failed to render vertical short.")
            
        return FileResponse(
            output_clip,
            media_type="video/mp4",
            filename="viral_short.mp4"
        )
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e)
        raise HTTPException(status_code=400, detail=f"Processing pipeline failed: {error_msg}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")

@app.get("/")
def read_root():
    return {"status": "ClipForge Backend is running with iOS/Web client configuration!"}