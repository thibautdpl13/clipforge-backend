import os
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Enable CORS for your Netlify frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for testing; can be restricted to your Netlify URL later
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
def process_video(request: VideoRequest):
    video_url = request.url
    
    if not video_url:
        raise HTTPException(status_code=400, HTTPException_detail="URL is required")

    # yt-dlp command array using cookies and client fallback to bypass 429 errors
    command = [
        "yt-dlp",
        "--dump-json",
        "--cookies", "cookies.txt",
        "--extractor-args", "youtube:player_client=android,web",
        video_url
    ]

    try:
        # Run yt-dlp as a subprocess
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        return {
            "success": True,
            "raw_output": result.stdout
        }

    except subprocess.CalledProcessError as e:
        # Capture yt-dlp errors for debugging
        error_output = e.stderr or e.stdout
        print(f"yt-dlp error: {error_output}")
        raise HTTPException(
            status_code=500,
            detail=f"Video processing failed: {error_output}"
        )