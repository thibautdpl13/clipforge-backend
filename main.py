import os
import glob
import subprocess
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def health_check():
    return {"status": "running", "service": "ClipForge AI Backend (Direct Upload Engine)"}

@app.post("/upload-and-clip")
async def upload_and_clip(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    input_video_path = f"/tmp/{file.filename}"
    output_path = "/tmp/clip_output.mp4"

    for f in [input_video_path, output_path]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

    try:
        with open(input_video_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {str(e)}")

    if not os.path.exists(input_video_path) or os.path.getsize(input_video_path) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty or missing.")

    duration = 60.0
    try:
        probe_cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", input_video_path
        ]
        res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout.strip():
            duration = float(res.stdout.strip())
    except Exception:
        pass

    clip_duration = min(duration, 45.0)
    chunk_duration = 3.0
    sample_phrases = [
        "CLIPFORGE AI",
        "DIRECT UPLOAD MODE",
        "SMOOTH 1080x1920",
        "PERFECT VERTICAL CLIP",
        "READY FOR TIKTOK & SHORTS"
    ]

    chunked_subtitles = []
    t = 0.0
    idx = 0
    while t < clip_duration:
        text = sample_phrases[idx % len(sample_phrases)]
        end_t = min(t + chunk_duration, clip_duration)
        chunked_subtitles.append({"start": round(t, 2), "end": round(end_t, 2), "text": text})
        t += chunk_duration
        idx += 1

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
        "-t", str(clip_duration),
        "-i", input_video_path,
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
        for f in [input_video_path, output_path]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    if background_tasks:
        background_tasks.add_task(cleanup)

    return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")