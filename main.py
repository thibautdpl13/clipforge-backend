import os
import io
import re
import time
import base64
import random
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Set, List, Tuple
from fastapi import FastAPI, File, UploadFile, Header, HTTPException, Depends, status, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field
import psycopg2
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
from jose import JWTError, jwt
import requests
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ==========================================
# 1. LOGGING & SECURITY CONSTANTS
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("clipforge.backend")

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clipforge_super_secure_secret_key_production_random_998877")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7       # 7 days valid access token
MAX_FILE_SIZE_BYTES = 250 * 1024 * 1024          # Strict 250MB upload cap
MAX_STORED_RENDER_BYTES = 10 * 1024 * 1024       # Cap DB history blob size at 10MB to protect RAM
CLIP_DURATION_SECONDS = 45.0
ALLOWED_VIDEO_MIME_TYPES: Set[str] = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-msvideo",
    "video/x-matroska"
}

ACTIVE_USER_JOBS: Set[str] = set()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="ClipForge AI Production Backend",
    description="Automated video clipping, JWT authentication, rate limiting, and compliance backend engine.",
    version="3.5.0",
    docs_url=None if os.getenv("ENVIRONMENT") == "production" else "/docs",
    redoc_url=None
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ==========================================
# 2. CORS & SECURITY HEADERS MIDDLEWARE
# ==========================================
origins = [
    "https://thibautdpl13.github.io",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:3000"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.middleware("http")
async def add_security_and_timing_headers(request: Request, call_next):
    start_time = time.time()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(f"Unhandled exception on {request.method} {request.url.path}: {str(exc)}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error. The incident has been logged."}
        )

    process_time_ms = round((time.time() - start_time) * 1000, 2)
    response.headers["X-Process-Time-Ms"] = str(process_time_ms)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' https://cdn.tailwindcss.com https://accounts.google.com https://*.googleapis.com https://*.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: data: https://clipforge-backend-4t73.onrender.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://accounts.google.com https://*.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com;"
    )

    if response.status_code >= 400:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(f"HTTP {response.status_code} | {request.method} {request.url.path} | IP: {client_ip} | {process_time_ms}ms")

    return response


# ==========================================
# 3. DATABASE CONNECTION & INDEX INITIALIZATION
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection() -> psycopg2.extensions.connection:
    if not DATABASE_URL:
        logger.critical("DATABASE_URL environment variable is missing.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database configuration error."
        )
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor,
            connect_timeout=10
        )
        return conn
    except Exception as e:
        logger.error(f"Database connection failure: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service temporarily unavailable. Please try again shortly."
        )

@app.on_event("startup")
def startup_db_initialization() -> None:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255),
                auth_provider VARCHAR(50) DEFAULT 'email',
                tier VARCHAR(50) DEFAULT 'free',
                clips_remaining INT DEFAULT 1,
                age_verified BOOLEAN DEFAULT FALSE,
                deletion_scheduled_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS id SERIAL UNIQUE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS age_verified BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMP DEFAULT NULL;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS renders (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source_filename VARCHAR(255) NOT NULL,
                video_b64 TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_deletion_scheduled ON users (deletion_scheduled_at) WHERE deletion_scheduled_at IS NOT NULL;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_renders_user_created ON renders (user_id, created_at DESC);")

        conn.commit()
        logger.info("Database schema and indexes verified successfully.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Startup DB Migration Error: {e}")
    finally:
        cur.close()
        conn.close()


# ==========================================
# 4. PYDANTIC DATA TRANSFER OBJECTS (MODELS)
# ==========================================
class EmailRegisterLoginSchema(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=8, max_length=128, description="Secure account password")
    age_verified: bool = Field(default=False, description="Confirmation that user meets minimum age requirement")
    cancel_deletion: bool = Field(default=False, description="Explicit confirmation to cancel pending 30-day deletion")

class GoogleAuthSchema(BaseModel):
    token: str = Field(..., min_length=10, description="Google OAuth credential ID token")
    age_verified: bool = Field(default=False, description="Confirmation of minimum age requirement")
    cancel_deletion: bool = Field(default=False, description="Explicit confirmation to cancel pending 30-day deletion")

class TokenResponseSchema(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]


# ==========================================
# 5. CRYPTOGRAPHY, CLEANUP & SUBTITLE HELPERS
# ==========================================
def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    if not hashed_password:
        return False
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def cleanup_temp_files(*filepaths: str) -> None:
    for path in filepaths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                logger.warning(f"Temp file cleanup warning for {path}: {e}")

def get_video_duration_seconds(filepath: str) -> float:
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                filepath
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20
        )
        if res.returncode == 0 and res.stdout.strip():
            dur = float(res.stdout.strip())
            if dur > 0:
                return dur
    except Exception as e:
        logger.warning(f"ffprobe duration lookup fallback: {e}")
    return CLIP_DURATION_SECONDS

def _clean_subtitle_text(raw_text: str) -> str:
    """Strips em/en/hyphen dashes, collapses whitespace, and formats clean uppercase caption text."""
    text = raw_text or ""
    text = text.replace("\u2014", " ").replace("\u2013", " ")
    text = re.sub(r"(^|\s)[-–—]+(\s|$)", " ", text)
    text = text.replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.upper()

def _build_window_cues(words: List[str], window_start: float, window_end: float) -> List[Tuple[float, float, str]]:
    """
    Builds short, crisp 2-4 word subtitle cues capped at ~1.65s each so captions
    stay on screen for a natural beat without lingering or overlapping later speech.
    """
    if not words:
        return []

    groups: List[List[str]] = []
    for i in range(0, len(words), 3):
        groups.append(words[i:i + 3])
    if len(groups) > 1 and len(groups[-1]) == 1:
        groups[-2].extend(groups[-1])
        groups.pop()

    cues: List[Tuple[float, float, str]] = []
    cursor = window_start
    for grp in groups:
        if cursor >= window_end - 0.25:
            break
        spoken_dur = max(0.95, min(1.65, len(grp) * 0.42))
        cue_end = min(window_end, cursor + spoken_dur)
        if cue_end - cursor >= 0.65:
            cues.append((cursor, cue_end, " ".join(grp)))
        cursor = cue_end + 0.05

    return cues

def _format_srt_timestamp(seconds: float) -> str:
    clamped = max(0.0, seconds)
    hours = int(clamped // 3600)
    minutes = int((clamped % 3600) // 60)
    secs = int(clamped % 60)
    millis = int(round((clamped - int(clamped)) * 1000))
    if millis == 1000:
        secs += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def generate_subtitles_srt(source_video_path: str, start_sec: float, duration_sec: float, srt_path: str) -> bool:
    """
    Transcribes the clipped segment in 1.6-second windows with low energy threshold
    for denser subtitle coverage without dashes or lingering cues.
    """
    try:
        import speech_recognition as sr  # type: ignore
    except ImportError:
        logger.warning("SpeechRecognition package not installed; skipping subtitles.")
        return False

    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = False
    recognizer.energy_threshold = 150

    cues: List[Tuple[float, float, str]] = []
    step_sec = 1.6
    offset = 0.0

    while offset < duration_sec - 0.25:
        chunk_len = min(step_sec, duration_sec - offset)
        chunk_wav = f"{srt_path}_{int(offset * 100)}.wav"
        res = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", f"{start_sec + offset:.2f}",
                "-t", f"{chunk_len:.2f}",
                "-i", source_video_path,
                "-vn", "-ac", "1", "-ar", "16000", chunk_wav
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if res.returncode == 0 and os.path.exists(chunk_wav):
            try:
                with sr.AudioFile(chunk_wav) as source:
                    audio_data = recognizer.record(source)
                raw_text = recognizer.recognize_google(audio_data, language="en-US")
                cleaned = _clean_subtitle_text(raw_text)
                if cleaned:
                    cues.extend(_build_window_cues(cleaned.split(), offset, offset + chunk_len))
            except sr.UnknownValueError:
                pass
            except Exception as e:
                logger.warning(f"Speech recognition chunk error at {offset}s: {e}")
            finally:
                cleanup_temp_files(chunk_wav)
        offset += step_sec

    if not cues:
        logger.info("No speech transcribed for clip segment.")
        return False

    try:
        with open(srt_path, "w", encoding="utf-8") as f:
            for idx, (s_sec, e_sec, txt) in enumerate(cues, start=1):
                f.write(f"{idx}\n")
                f.write(f"{_format_srt_timestamp(s_sec)} --> {_format_srt_timestamp(e_sec)}\n")
                f.write(f"{txt}\n\n")
        return True
    except Exception as e:
        logger.warning(f"Failed writing SRT file: {e}")
        return False

def _escape_ffmpeg_filter_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    normalized = normalized.replace(":", "\\:").replace("'", "\\'")
    return normalized

def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication token header."
        )

    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token payload missing subject identifier."
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid credentials. Please sign in again."
        )

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, email, auth_provider, tier, clips_remaining, age_verified, deletion_scheduled_at FROM users WHERE email = %s;",
            (email,)
        )
        user = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account no longer exists."
        )

    if user["deletion_scheduled_at"]:
        if datetime.utcnow() >= user["deletion_scheduled_at"]:
            purge_conn = get_db_connection()
            purge_cur = purge_conn.cursor()
            try:
                purge_cur.execute("DELETE FROM users WHERE email = %s;", (email,))
                purge_conn.commit()
            finally:
                purge_cur.close()
                purge_conn.close()

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account has been permanently purged following the 30-day grace period."
            )
        else:
            formatted_dt = user["deletion_scheduled_at"].strftime("%Y-%m-%d at %H:%M UTC")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account deletion is scheduled for {formatted_dt}. Sign in again and confirm cancellation to restore access."
            )

    return user


# ==========================================
# 6. API ROUTE ENDPOINTS: HEALTH & AUTH
# ==========================================
@app.api_route("/", methods=["GET", "HEAD"], tags=["Health"])
@app.get("/health", tags=["Health"])
def health_check():
    db_status = "connected"
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.close()
        conn.close()
    except Exception:
        db_status = "degraded"

    return {
        "status": "online",
        "service": "ClipForge AI Backend",
        "version": "3.5.0",
        "database": db_status,
        "timestamp_utc": datetime.utcnow().isoformat()
    }


@app.post("/auth/register-login", response_model=TokenResponseSchema, tags=["Authentication"])
@limiter.limit("10/minute")
def register_or_login_email_endpoint(request: Request, data: EmailRegisterLoginSchema):
    normalized_email = data.email.strip().lower()
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT * FROM users WHERE email = %s;", (normalized_email,))
        user = cur.fetchone()

        if user:
            if not user["password_hash"] or not verify_password(data.password, user["password_hash"]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Incorrect password or account was originally registered via Google Sign-In."
                )

            if user["deletion_scheduled_at"]:
                if datetime.utcnow() < user["deletion_scheduled_at"]:
                    if not data.cancel_deletion:
                        formatted_dt = user["deletion_scheduled_at"].strftime("%B %d, %Y at %H:%M UTC")
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "code": "DELETION_PENDING",
                                "scheduled_at": user["deletion_scheduled_at"].isoformat(),
                                "formatted_date": formatted_dt,
                                "message": f"Your account deletion is scheduled for {formatted_dt}. Cancel deletion and restore your account?"
                            }
                        )
                    else:
                        cur.execute("UPDATE users SET deletion_scheduled_at = NULL WHERE email = %s;", (normalized_email,))
                        conn.commit()
                        cur.execute("SELECT * FROM users WHERE email = %s;", (normalized_email,))
                        user = cur.fetchone()
                else:
                    cur.execute("DELETE FROM users WHERE email = %s;", (normalized_email,))
                    conn.commit()
                    user = None

        if not user:
            if not data.age_verified:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You must confirm that you meet the minimum age requirement to create an account."
                )

            hashed_pw = get_password_hash(data.password)
            cur.execute(
                """INSERT INTO users (email, password_hash, auth_provider, tier, clips_remaining, age_verified)
                   VALUES (%s, %s, 'email', 'free', 1, %s) RETURNING *;""",
                (normalized_email, hashed_pw, data.age_verified)
            )
            user = cur.fetchone()
            conn.commit()
            logger.info(f"New email account provisioned: {normalized_email}")

        access_token = create_access_token(data={"sub": user["email"]})
        user_payload = {
            "email": user["email"],
            "tier": user["tier"],
            "clips_remaining": user["clips_remaining"],
            "auth_provider": user["auth_provider"],
            "deletion_scheduled_at": None
        }

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user_payload
        }

    finally:
        cur.close()
        conn.close()


@app.post("/api/auth/google", response_model=TokenResponseSchema, tags=["Authentication"])
@limiter.limit("15/minute")
def google_auth_endpoint(request: Request, data: GoogleAuthSchema):
    try:
        google_response = requests.get(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={data.token}",
            timeout=8
        )
        google_data = google_response.json()
        if "email" not in google_data:
            raise HTTPException(status_code=400, detail="Google token verification failed.")
        email = google_data["email"].strip().lower()
    except requests.Timeout:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Google authentication service timed out. Please retry."
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to validate Google authentication token."
        )

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT * FROM users WHERE email = %s;", (email,))
        user = cur.fetchone()

        if user and user["deletion_scheduled_at"]:
            if datetime.utcnow() < user["deletion_scheduled_at"]:
                if not data.cancel_deletion:
                    formatted_dt = user["deletion_scheduled_at"].strftime("%B %d, %Y at %H:%M UTC")
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "DELETION_PENDING",
                            "scheduled_at": user["deletion_scheduled_at"].isoformat(),
                            "formatted_date": formatted_dt,
                            "message": f"Your account deletion is scheduled for {formatted_dt}. Cancel deletion and restore your account?"
                        }
                    )
                else:
                    cur.execute("UPDATE users SET deletion_scheduled_at = NULL WHERE email = %s;", (email,))
                    conn.commit()
                    cur.execute("SELECT * FROM users WHERE email = %s;", (email,))
                    user = cur.fetchone()
            else:
                cur.execute("DELETE FROM users WHERE email = %s;", (email,))
                conn.commit()
                user = None

        if not user:
            if not data.age_verified:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You must confirm that you meet the minimum age requirement."
                )

            cur.execute(
                """INSERT INTO users (email, password_hash, auth_provider, tier, clips_remaining, age_verified)
                   VALUES (%s, NULL, 'google', 'free', 1, %s) RETURNING *;""",
                (email, data.age_verified)
            )
            user = cur.fetchone()
            conn.commit()
            logger.info(f"New Google OAuth account provisioned: {email}")

        access_token = create_access_token(data={"sub": user["email"]})
        user_payload = {
            "email": user["email"],
            "tier": user["tier"],
            "clips_remaining": user["clips_remaining"],
            "auth_provider": user["auth_provider"],
            "deletion_scheduled_at": None
        }

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user_payload
        }

    finally:
        cur.close()
        conn.close()


# ==========================================
# 7. ACCOUNT LIFECYCLE & RENDER HISTORY ENDPOINTS
# ==========================================
@app.post("/api/account/schedule-deletion", tags=["Account Lifecycle"])
@limiter.limit("5/minute")
def schedule_account_deletion_endpoint(request: Request, current_user: Dict[str, Any] = Depends(get_current_user)):
    deletion_time = datetime.utcnow() + timedelta(days=30)
    formatted_dt = deletion_time.strftime("%B %d, %Y at %H:%M UTC")

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE users SET deletion_scheduled_at = %s WHERE email = %s;",
            (deletion_time, current_user["email"])
        )
        conn.commit()
        logger.info(f"Account scheduled for deletion: {current_user['email']} at {formatted_dt}")
    finally:
        cur.close()
        conn.close()

    return {
        "status": "scheduled",
        "deletion_scheduled_at": deletion_time.isoformat(),
        "formatted_date": formatted_dt,
        "message": f"Account deletion is scheduled for {formatted_dt}."
    }


@app.get("/api/renders", tags=["Video Processing"])
def list_user_renders_endpoint(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT id, source_filename, created_at
               FROM renders
               WHERE user_id = %s
               ORDER BY created_at DESC
               LIMIT 10;""",
            (current_user["id"],)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    return {
        "renders": [
            {
                "id": r["id"],
                "source_filename": r["source_filename"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None
            }
            for r in rows
        ]
    }


@app.get("/api/renders/{render_id}/video", tags=["Video Processing"])
def stream_saved_render_endpoint(
    render_id: int,
    download: bool = False,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, source_filename, video_b64 FROM renders WHERE id = %s AND user_id = %s;",
            (render_id, current_user["id"])
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Render not found.")

    video_bytes = base64.b64decode(row["video_b64"])
    headers = {}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="clipforge_vertical_highlight.mp4"'

    return StreamingResponse(
        io.BytesIO(video_bytes),
        media_type="video/mp4",
        headers=headers
    )


# ==========================================
# 8. CORE VIDEO CLIPPING PIPELINE ENDPOINT
# ==========================================
@app.post("/upload-and-clip", tags=["Video Processing"])
@limiter.limit("6/minute")
async def upload_and_clip_endpoint(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    user_email = current_user["email"]

    if user_email in ACTIVE_USER_JOBS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A video is already processing on your account. Please wait for it to finish."
        )

    if file.content_type not in ALLOWED_VIDEO_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{file.content_type}'. Allowed formats: MP4, MOV, WebM."
        )

    if current_user["clips_remaining"] <= 0:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No remaining clip credits available. Please upgrade your subscription tier."
        )

    ACTIVE_USER_JOBS.add(user_email)
    job_identifier = os.urandom(6).hex()
    input_filepath = f"input_{job_identifier}.mp4"
    output_filepath = f"output_{job_identifier}.mp4"
    srt_filepath = f"subs_{job_identifier}.srt"

    written_bytes = 0
    try:
        with open(input_filepath, "wb") as buffer_file:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written_bytes += len(chunk)
                if written_bytes > MAX_FILE_SIZE_BYTES:
                    buffer_file.close()
                    cleanup_temp_files(input_filepath, srt_filepath)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Uploaded file exceeds the maximum permitted size limit of 250MB."
                    )
                buffer_file.write(chunk)
    except HTTPException:
        ACTIVE_USER_JOBS.discard(user_email)
        raise
    except Exception:
        cleanup_temp_files(input_filepath, srt_filepath)
        ACTIVE_USER_JOBS.discard(user_email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to read uploaded file."
        )

    if written_bytes == 0:
        cleanup_temp_files(input_filepath, srt_filepath)
        ACTIVE_USER_JOBS.discard(user_email)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty."
        )

    try:
        total_duration = get_video_duration_seconds(input_filepath)
        clip_len = min(CLIP_DURATION_SECONDS, total_duration) if total_duration > 0 else CLIP_DURATION_SECONDS
        max_start = max(0.0, total_duration - clip_len)
        start_sec = round(random.uniform(0.0, max_start), 2) if max_start > 0 else 0.0

        has_subs = generate_subtitles_srt(input_filepath, start_sec, clip_len, srt_filepath)

        # Full-frame 9:16 portrait letterbox with black bars on top/bottom and zero cutting.
        # MarginV=535 leaves a clean gap below the 16:9 frame while keeping captions in the top of the bottom black bar.
        vf_chain = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        if has_subs and os.path.exists(srt_filepath):
            escaped_srt = _escape_ffmpeg_filter_path(srt_filepath)
            vf_chain += (
                f",subtitles='{escaped_srt}':force_style="
                "'PlayResX=1080,PlayResY=1920,FontName=Arial,FontSize=54,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=4,Shadow=0,Alignment=2,MarginV=535,MarginL=50,MarginR=50'"
            )

        ffmpeg_command = [
            "ffmpeg", "-y", "-threads", "2",
            "-ss", f"{start_sec:.2f}",
            "-i", input_filepath,
            "-t", f"{clip_len:.2f}",
            "-vf", vf_chain,
            "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "24",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", output_filepath
        ]

        process_result = subprocess.run(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if process_result.returncode != 0:
            logger.error(f"FFmpeg error for {user_email}: {process_result.stderr[-300:]}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Video rendering failed due to an invalid stream or codec."
            )

        output_size = os.path.getsize(output_filepath)
        encoded_video = None
        if output_size <= MAX_STORED_RENDER_BYTES:
            with open(output_filepath, "rb") as out_f:
                encoded_video = base64.b64encode(out_f.read()).decode("utf-8")

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            updated_credits = max(0, current_user["clips_remaining"] - 1)
            updated_tier = "free_used" if current_user["tier"] in ("free", "demo") else current_user["tier"]

            cur.execute(
                "UPDATE users SET clips_remaining = %s, tier = %s WHERE email = %s;",
                (updated_credits, updated_tier, user_email)
            )

            if encoded_video:
                safe_name = (file.filename or "clip.mp4")[-240:]
                cur.execute(
                    """INSERT INTO renders (user_id, source_filename, video_b64)
                       VALUES (%s, %s, %s);""",
                    (current_user["id"], safe_name, encoded_video)
                )
                cur.execute(
                    """DELETE FROM renders
                       WHERE user_id = %s
                         AND id NOT IN (
                             SELECT id FROM renders WHERE user_id = %s ORDER BY created_at DESC LIMIT 10
                         );""",
                    (current_user["id"], current_user["id"])
                )

            conn.commit()
        finally:
            cur.close()
            conn.close()

        del encoded_video
        background_tasks.add_task(cleanup_temp_files, input_filepath, output_filepath, srt_filepath)

        return FileResponse(
            output_filepath,
            media_type="video/mp4",
            filename="clipforge_vertical_highlight.mp4"
        )

    except Exception as err:
        cleanup_temp_files(input_filepath, output_filepath, srt_filepath)
        if isinstance(err, HTTPException):
            raise err
        logger.error(f"Unexpected error clipping video for {user_email}: {str(err)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred during clip generation."
        )
    finally:
        ACTIVE_USER_JOBS.discard(user_email)