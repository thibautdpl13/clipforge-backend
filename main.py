from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import json
import os

app = FastAPI()

# Enable CORS so your Netlify frontend can talk to this Render backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for testing; restrict to your netlify domain later if desired
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoRequest(BaseModel):
    url: str

@app.get("/")
def read_root():
    return {"status": "ClipForge Backend is running!"}

@app.post("/process-video")
def process_video(data: VideoRequest):
    video_url = data.url
    
    if not video_url:
        raise HTTPException(status_code=400, detail="No URL provided")

    # yt-dlp command using extractor_args to bypass YouTube bot / sign-in walls via alternative clients
    command = [
        "yt-dlp",
        "--dump-json",
        "--extractor-args", "youtube:player_client=android,web",
        video_url
    ]

    try:
        # Run yt-dlp as a subprocess to extract video metadata and direct stream links
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        # Parse the JSON output from yt-dlp
        video_info = json.loads(result.stdout)
        
        return {
            "success": True,
            "title": video_info.get("title"),
            "duration": video_info.get("duration"),
            "thumbnail": video_info.get("thumbnail"),
            "download_url": video_info.get("url")  # Direct playback/download link
        }

    except subprocess.CalledProcessError as e:
        error_message = e.stderr.strip() if e.stderr else str(e)
        raise HTTPException(
            status_code=500, 
            detail=f"Video processing failed: {error_message}"
        )
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500, 
            detail="Failed to parse video metadata from yt-dlp."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"Unexpected server error: {str(e)}"
        )