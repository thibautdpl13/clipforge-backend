import os
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from pydantic import BaseModel

app = FastAPI()

# Allow requests from your GitHub Pages frontend and local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ClipRequest(BaseModel):
    url: str

def download_and_process_video(video_url: str, output_path: str):
    """
    Downloads the YouTube video using yt-dlp with cookie authentication 
    and browser headers, then applies your clipping/processing pipeline.
    """
    raw_download_path = "/tmp/raw_download.mp4"
    
    # Cleanup previous temp files
    for p in [raw_download_path, output_path]:
        if os.path.exists(p):
            os.remove(p)

    # yt-dlp configuration with cookies.txt and browser client bypasses
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=mp4]/best[ext=mp4]/best',
        'outtmpl': raw_download_path,
        'noplaylist': True,
        'cookiefile': 'cookies.txt',  # Uses your cookies.txt in the backend root
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
        },
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}}
    }

    try:
        # Step 1: Download video with yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        if not os.path.exists(raw_download_path):
            raise Exception("yt-dlp failed to generate raw download file.")

        # Step 2: Your video processing / vertical cropping / clipping logic 
        # (e.g., using ffmpeg to crop 16:9 to 9:16 vertical or trim the clip)
        # If you have custom ffmpeg commands or script calls, place them here:
        
        # Example FFmpeg vertical crop command (center crop 9:16):
        ffmpeg_command = [
            'ffmpeg', '-y', '-i', raw_download_path,
            '-vf', "crop=ih*9/16:ih",  # Crop to vertical 9:16 aspect ratio
            '-c:a', 'copy',
            output_path
        ]
        
        subprocess.run(ffmpeg_command, check=True)

        # Fallback if processing didn't output the final path but raw download exists
        if not os.path.exists(output_path) and os.path.exists(raw_download_path):
            os.rename(raw_download_path, output_path)

    except Exception as e:
        raise Exception(f"Pipeline failure: {str(e)}")

@app.post("/generate-auto-clip")
async def generate_auto_clip(payload: ClipRequest):
    video_url = payload.url
    if not video_url:
        raise HTTPException(status_code=400, detail="URL is required")

    final_output_path = "/tmp/clip_output.mp4"

    try:
        download_and_process_video(video_url, final_output_path)
        
        if not os.path.exists(final_output_path):
            raise HTTPException(status_code=500, detail="Processing completed, but output file is missing.")

        return FileResponse(final_output_path, media_type="video/mp4", filename="clip.mp4")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)