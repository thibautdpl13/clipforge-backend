import os
import io
import re
import time
import base64
import random
import logging
import subprocess
import urllib.request
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Set, List
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
from faster_whisper import WhisperModel

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

FONT_PATH = "/tmp/heavy_font.ttf"
MODEL_SIZE = "tiny"
whisper_model: Optional[WhisperModel] = None

# In-memory active processing lock to prevent duplicate simultaneous submissions per user
ACTIVE_USER_JOBS: Set[str] = set()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="ClipForge AI Production Backend",
    description="Automated video clipping, JWT authentication, rate limiting, and compliance backend engine.",
    version="10.9.5",
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
# 3. DATABASE CONNECTION & STARTUP INIT
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

def get_whisper_model() -> Optional[WhisperModel]:
    global whisper_model
    if whisper_model is None:
        try:
            # cpu_threads=1 keeps RAM usage strictly flat and low
            whisper_model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=1)
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
    return whisper_model

def ensure_heavy_font() -> str:
    if not os.path.exists(FONT_PATH):
        try:
            font_url = "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat-Black.ttf"
            urllib.request.urlretrieve(font_url, FONT_PATH)
        except Exception:
            try:
                fallback_url = "https://github.com/google/fonts/raw/main/ofl/robotoblack/Roboto-Black.ttf"
                urllib.request.urlretrieve(fallback_url, FONT_PATH)
            except Exception:
                pass
    if not os.path.exists(FONT_PATH):
        return "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    return FONT_PATH

@app.on_event("startup")
def startup_db_and_engine_initialization() -> None:
    ensure_heavy_font()
    get_whisper_model()

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
        logger.info("Database schema, indexes, font, and Whisper model initialized successfully.")
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
# 5. CRYPTOGRAPHY, CLEANUP & AUTH HELPERS
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
        "version": "10.9.5",
        "model": MODEL_SIZE,
        "font": ensure_heavy_font(),
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
# 7. ACCOUNT LIFECYCLE & RENDER PREVIEW ENDPOINTS
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
    """Returns metadata for the current user's recent vertical renders (without spending credits)."""
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
    """Streams a previously rendered vertical clip for preview or download without deducting credits."""
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
        headers["Content-Disposition"] = 'attachment; filename="vertical_clip.mp4"'

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
    job_id = os.urandom(6).hex()
    safe_upload_name = os.path.basename(file.filename or f"upload_{job_id}.mp4")
    input_video_path = f"/tmp/input_{job_id}_{safe_upload_name}"
    slice_video_path = f"/tmp/slice_{job_id}_{os.getpid()}.mp4"
    audio_wav_path = f"/tmp/audio_{job_id}_{os.getpid()}.wav"
    output_path = f"/tmp/clip_output_{job_id}_{os.getpid()}.mp4"

    all_temp_files = [input_video_path, slice_video_path, audio_wav_path, output_path]
    cleanup_temp_files(*all_temp_files)

    written_bytes = 0
    try:
        with open(input_video_path, "wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written_bytes += len(chunk)
                if written_bytes > MAX_FILE_SIZE_BYTES:
                    buffer.close()
                    cleanup_temp_files(*all_temp_files)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Uploaded file exceeds the maximum permitted size limit of 250MB."
                    )
                buffer.write(chunk)
    except HTTPException:
        ACTIVE_USER_JOBS.discard(user_email)
        raise
    except Exception as e:
        cleanup_temp_files(*all_temp_files)
        ACTIVE_USER_JOBS.discard(user_email)
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {str(e)}")

    if not os.path.exists(input_video_path) or os.path.getsize(input_video_path) == 0:
        cleanup_temp_files(*all_temp_files)
        ACTIVE_USER_JOBS.discard(user_email)
        raise HTTPException(status_code=400, detail="Uploaded file is empty or missing.")

    try:
        # 1. Get full video duration
        duration = 35.0
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

        # 2. Pick a random start time anywhere in the video for a 30s-45s clip snippet
        clip_duration = min(duration, CLIP_DURATION_SECONDS)
        max_start = max(0.0, duration - clip_duration)
        start_time = random.uniform(0.0, max_start) if max_start > 0 else 0.0

        # 3. Quickly slice the video snippet first using FFmpeg (zero RAM usage)
        try:
            slice_cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", input_video_path,
                "-t", str(clip_duration),
                "-c", "copy",
                slice_video_path
            ]
            res = subprocess.run(slice_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode != 0 or not os.path.exists(slice_video_path) or os.path.getsize(slice_video_path) == 0:
                slice_cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(start_time),
                    "-i", input_video_path,
                    "-t", str(clip_duration),
                    slice_video_path
                ]
                subprocess.run(slice_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Video slicing failed: {str(e)}")

        # 4. Extract audio ONLY from this small slice (tiny file size, zero RAM pressure)
        try:
            extract_cmd = [
                "ffmpeg", "-y", "-i", slice_video_path,
                "-ar", "16000", "-ac", "1", audio_wav_path
            ]
            subprocess.run(extract_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Audio extraction failed: {str(e)}")

        # 5. Run Whisper on the lightweight slice audio (stays safely under 512MB RAM)
        raw_word_list: List[Dict[str, Any]] = []
        model = get_whisper_model()

        if model and os.path.exists(audio_wav_path):
            try:
                segments, _ = model.transcribe(
                    audio_wav_path,
                    word_timestamps=True,
                    beam_size=1,
                    vad_filter=True,
                    condition_on_previous_text=False
                )
                for segment in segments:
                    if hasattr(segment, "words") and segment.words:
                        for w in segment.words:
                            raw_w = w.word.strip()
                            clean_w = re.sub(r"[^a-zA-Z0-9\$\'\,\s\?\!]", "", raw_w).strip()
                            if clean_w:
                                raw_word_list.append({
                                    "start": float(w.start),
                                    "end": float(w.end),
                                    "text": clean_w.upper()
                                })
            except Exception as e:
                logger.error(f"Whisper transcription error: {e}")

        # 1. Merge split number/comma tokens (e.g. "100" and ",000" -> "100,000")
        merged_list: List[Dict[str, Any]] = []
        for w in raw_word_list:
            if merged_list and (w["text"].startswith(",") or w["text"] == ","):
                merged_list[-1]["text"] += w["text"]
                merged_list[-1]["end"] = w["end"]
            else:
                merged_list.append(w)

        # 2. Strip useless "A " before dollar amounts (e.g., "A $250,000" -> "$250,000")
        word_timed_list: List[Dict[str, Any]] = []
        i = 0
        while i < len(merged_list):
            curr = merged_list[i]
            if curr["text"] == "A" and i + 1 < len(merged_list) and merged_list[i + 1]["text"].startswith("$"):
                next_token = merged_list[i + 1]
                next_token["start"] = curr["start"]
                word_timed_list.append(next_token)
                i += 2
            else:
                word_timed_list.append(curr)
                i += 1

        if not word_timed_list:
            word_timed_list = [
                {"start": 0.0, "end": clip_duration, "text": "CLIPFORGE AI"}
            ]

        font_file = ensure_heavy_font()

        # Intelligent Word Grouping into Reader-Friendly Chunks (Max 3-4 words)
        chunked_subtitles: List[Dict[str, Any]] = []
        current_chunk: List[str] = []
        chunk_start: Optional[float] = None

        for w in word_timed_list:
            if not current_chunk:
                chunk_start = w["start"]
            current_chunk.append(w["text"])

            if len(current_chunk) >= 3 or len(" ".join(current_chunk)) > 16:
                chunked_subtitles.append({
                    "start": round(chunk_start, 2),
                    "end": round(w["end"], 2),
                    "text": " ".join(current_chunk)
                })
                current_chunk = []

        if current_chunk and chunk_start is not None:
            chunked_subtitles.append({
                "start": round(chunk_start, 2),
                "end": round(word_timed_list[-1]["end"], 2),
                "text": " ".join(current_chunk)
            })

        vf_filters = [
            "scale=1080:1920:force_original_aspect_ratio=decrease",
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
        ]

        for sub in chunked_subtitles:
            safe_text = sub["text"].replace("'", "").replace(":", "\\:").replace("%", "%%").replace('"', "")
            draw_filter = (
                f"drawtext=text='{safe_text}':"
                f"fontfile={font_file}:"
                f"fontsize=52:fontcolor=white:"
                f"borderw=5:bordercolor=black:"
                f"shadowx=2:shadowy=2:shadowcolor=black@1.0:"
                f"x=(w-text_w)/2:y=1300:"
                f"enable='between(t,{sub['start']},{sub['end']})'"
            )
            vf_filters.append(draw_filter)

        video_filter = ",".join(vf_filters)

        # 6. Final render on the already-sliced video snippet
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", slice_video_path,
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "22",
            "-threads", "2",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]

        ff_res = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if ff_res.returncode != 0 or not os.path.exists(output_path):
            ff_err = ff_res.stderr.decode("utf-8", errors="ignore")
            raise HTTPException(status_code=500, detail=f"FFmpeg rendering failed: {ff_err[-300:]}")

        output_size = os.path.getsize(output_path)
        encoded_video = None
        if output_size <= MAX_STORED_RENDER_BYTES:
            with open(output_path, "rb") as out_f:
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

        if background_tasks:
            background_tasks.add_task(cleanup_temp_files, *all_temp_files)

        return FileResponse(output_path, media_type="video/mp4", filename="vertical_clip.mp4")

    except Exception as err:
        cleanup_temp_files(*all_temp_files)
        if isinstance(err, HTTPException):
            raise err
        logger.error(f"Unexpected error clipping video for {user_email}: {str(err)}")
        raise HTTPException(status_code=500, detail=f"Clip generation failed: {str(err)}")
    finally:
        ACTIVE_USER_JOBS.discard(user_email)