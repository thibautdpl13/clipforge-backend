import subprocess
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Enable CORS for your Netlify frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class VideoURL(BaseModel):
    url: str

@app.post("/process-video")
async def process_video(payload: VideoURL):
    video_url = payload.url
    
    # yt-dlp command using web client and cookies
    command = [
        "yt-dlp",
        "--dump-json",
        "--cookies", "cookies.txt",
        "--extractor-args", "youtube:player_client=web",
        video_url
    ]
    
    try:
        # Run yt-dlp and capture output
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        # Parse the JSON metadata returned by yt-dlp
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

@app.get("/")
def read_root():
    return {"status": "ClipForge Backend is running!"}