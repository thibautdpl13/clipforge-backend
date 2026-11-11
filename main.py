import os
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="ClipForge Backend")

# Allow requests from your Netlify frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ClipRequest(BaseModel):
    url: str

@app.get("/")
def read_root():
    return {"status": "ClipForge Backend is running perfectly!"}

@app.post("/process-clip")
async def process_clip(data: ClipRequest):
    if not data.url or not data.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")

    output_template = "source.mp4"
    
    # Remove existing file if present to avoid overwrite conflicts
    if os.path.exists(output_template):
        os.remove(output_template)

    try:
        # Run yt-dlp to download the video with format fallback
        result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "-f", "b/best[ext=mp4]/best",
                "-o", output_template,
                data.url.strip()
            ],
            capture_output=True,
            text=True,
            check=True
        )
        return {
            "status": "success",
            "message": "Video processed successfully!",
            "output_file": output_template
        }
    except subprocess.CalledProcessError as e:
        error_details = e.stderr.strip() if e.stderr else str(e)
        print(f"yt-dlp error: {error_details}")
        raise HTTPException(
            status_code=500,
            detail=f"Video processing failed: {error_details}"
        )