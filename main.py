import os
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import FastAPI, File, UploadFile, Header, HTTPException, Depends, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
import psycopg2
from psycopg2.extras import RealDictCursor
from passlib.context import CryptContext
from jose import JWTError, jwt
import requests

# ==========================================
# 1. SECURITY & CONFIGURATION CONSTANTS
# ==========================================
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "clipforge_super_secure_secret_key_change_in_prod_998877")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days valid access token
MAX_FILE_SIZE_BYTES = 250 * 1024 * 1024     # Strict 250MB upload cap

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(
    title="ClipForge AI Production Backend",
    description="Automated video clipping, JWT authentication, and compliance backend engine.",
    version="2.2.0"
)

# ==========================================
# 2. CORS MIDDLEWARE CONFIGURATION
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
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 3. DATABASE CONNECTION & MIGRATION UTILS
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection() -> psycopg2.extensions.connection:
    """Establishes and returns a live connection to the PostgreSQL database."""
    if not DATABASE_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Critical: DATABASE_URL environment variable is not configured."
        )
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database connection error: {str(e)}"
        )

@app.on_event("startup")
def startup_db_initialization() -> None:
    """Ensures required PostgreSQL tables and columns exist upon application startup."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255),
                auth_provider VARCHAR(50) DEFAULT 'email',
                tier VARCHAR(50) DEFAULT 'demo',
                clips_remaining INT DEFAULT 1,
                age_verified BOOLEAN DEFAULT FALSE,
                deletion_scheduled_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Safe column additions in case table already existed from v1
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS age_verified BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMP DEFAULT NULL;")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Startup DB Migration Warning: {e}")
    finally:
        cur.close()
        conn.close()


# ==========================================
# 4. PYDANTIC DATA TRANSFER OBJECTS (MODELS)
# ==========================================
class EmailRegisterLoginSchema(BaseModel):
    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=8, description="Secure account password")
    age_verified: bool = Field(default=False, description="Confirmation that user meets minimum age requirement")
    cancel_deletion: bool = Field(default=False, description="Explicit confirmation to cancel pending 30-day deletion")

class GoogleAuthSchema(BaseModel):
    token: str = Field(..., description="Google OAuth credential ID token")
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
    """Verifies a plaintext password against its bcrypt hash."""
    if not hashed_password:
        return False
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Generates a secure bcrypt password hash."""
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Encodes a signed JWT access token containing user claims."""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def cleanup_temp_files(*filepaths: str) -> None:
    """Background task helper to remove temporary video files after streaming response."""
    for path in filepaths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Cleanup warning for {path}: {e}")

def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """Dependency validator that decodes JWT tokens and checks account deletion status."""
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
            detail="Could not validate authentication credentials or token has expired."
        )

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM users WHERE email = %s;", (email,))
        user = cur.fetchone()
    finally:
        cur.close()
        conn.close()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User account associated with token no longer exists."
        )

    # Check 30-day scheduled account deletion status
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
                detail=f"Account deletion is scheduled for {formatted_dt}. Please log in again and confirm cancellation to restore access."
            )

    return user


# ==========================================
# 6. API ROUTE ENDPOINTS: AUTHENTICATION
# ==========================================
@app.get("/", tags=["Health"])
def health_check():
    """Health check endpoint for Render deployment monitoring."""
    return {"status": "online", "service": "ClipForge AI Backend", "version": "2.2.0"}


@app.post("/auth/register-login", response_model=TokenResponseSchema, tags=["Authentication"])
def register_or_login_email_endpoint(data: EmailRegisterLoginSchema):
    """Handles secure user registration and login via email and password."""
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("SELECT * FROM users WHERE email = %s;", (data.email,))
        user = cur.fetchone()

        if user:
            # Verify password first so unauthorized people cannot probe deletion dates
            if not user["password_hash"] or not verify_password(data.password, user["password_hash"]):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Incorrect password or account was originally registered via Google Sign-In."
                )

            # Handle pending 30-day scheduled deletion
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
                                "message": f"Your account deletion is scheduled for {formatted_dt}. Would you like to cancel the deletion and restore your account with all your data?"
                            }
                        )
                    else:
                        # User chose to cancel deletion and restore their account
                        cur.execute("UPDATE users SET deletion_scheduled_at = NULL WHERE email = %s;", (data.email,))
                        conn.commit()
                        cur.execute("SELECT * FROM users WHERE email = %s;", (data.email,))
                        user = cur.fetchone()
                else:
                    # 30 days elapsed: permanently delete old record, allow fresh registration
                    cur.execute("DELETE FROM users WHERE email = %s;", (data.email,))
                    conn.commit()
                    user = None

        if not user:
            # New account registration workflow
            if not data.age_verified:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="You must confirm that you meet the minimum age requirement to create an account."
                )

            hashed_pw = get_password_hash(data.password)
            cur.execute(
                """INSERT INTO users (email, password_hash, auth_provider, tier, clips_remaining, age_verified)
                   VALUES (%s, %s, 'email', 'demo', 1, %s) RETURNING *;""",
                (data.email, hashed_pw, data.age_verified)
            )
            user = cur.fetchone()
            conn.commit()

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
def google_auth_endpoint(data: GoogleAuthSchema):
    """Verifies Google ID tokens and provisions or authenticates users without requiring a password."""
    try:
        google_response = requests.get(f"https://oauth2.googleapis.com/tokeninfo?id_token={data.token}", timeout=10)
        google_data = google_response.json()
        if "email" not in google_data:
            raise HTTPException(status_code=400, detail="Google token verification failed.")
        email = google_data["email"]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to validate Google authentication token with provider."
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
                            "message": f"Your account deletion is scheduled for {formatted_dt}. Would you like to cancel the deletion and restore your account with all your data?"
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
                   VALUES (%s, NULL, 'google', 'demo', 1, %s) RETURNING *;""",
                (email, data.age_verified)
            )
            user = cur.fetchone()
            conn.commit()

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
# 7. ACCOUNT LIFECYCLE MANAGEMENT ENDPOINTS
# ==========================================
@app.post("/api/account/schedule-deletion", tags=["Account Lifecycle"])
def schedule_account_deletion_endpoint(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Schedules account deletion exactly 30 days from now down to the minute."""
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
    finally:
        cur.close()
        conn.close()

    return {
        "status": "scheduled",
        "deletion_scheduled_at": deletion_time.isoformat(),
        "formatted_date": formatted_dt,
        "message": f"Account deletion is scheduled for {formatted_dt} (30 days from now). If you log back in before then and choose to cancel deletion, your account and data will be restored."
    }


# ==========================================
# 8. CORE VIDEO CLIPPING PIPELINE ENDPOINT
# ==========================================
@app.post("/upload-and-clip", tags=["Video Processing"])
async def upload_and_clip_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Secure video clipping endpoint with file size limits, credit verification, and FFmpeg processing."""
    file_contents = await file.read()
    if len(file_contents) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Uploaded file exceeds the maximum permitted size limit of 250MB."
        )

    # Credit consumption validation
    if current_user["clips_remaining"] <= 0 and current_user["tier"] != "agency":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No remaining clip credits available. Please upgrade your subscription tier."
        )

    job_identifier = os.urandom(6).hex()
    input_filepath = f"input_{job_identifier}.mp4"
    output_filepath = f"output_{job_identifier}.mp4"

    with open(input_filepath, "wb") as buffer_file:
        buffer_file.write(file_contents)

    try:
        # High-performance FFmpeg rendering command for short-form vertical conversion
        ffmpeg_command = [
            "ffmpeg", "-y", "-i", input_filepath,
            "-ss", "00:00:00", "-to", "00:00:30",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", output_filepath
        ]

        process_result = subprocess.run(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if process_result.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"FFmpeg rendering execution failed: {process_result.stderr[-200:]}"
            )

        # Deduct credit and update tier usage in database atomically
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            updated_credits = max(0, current_user["clips_remaining"] - 1) if current_user["tier"] != "agency" else current_user["clips_remaining"]
            updated_tier = "demo_used" if current_user["tier"] == "demo" else current_user["tier"]

            cur.execute(
                "UPDATE users SET clips_remaining = %s, tier = %s WHERE email = %s;",
                (updated_credits, updated_tier, current_user["email"])
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()

        # Schedule automatic cleanup of temporary server files after sending response
        background_tasks.add_task(cleanup_temp_files, input_filepath, output_filepath)

        return FileResponse(
            output_filepath,
            media_type="video/mp4",
            filename="clipforge_vertical_highlight.mp4"
        )

    except Exception as err:
        cleanup_temp_files(input_filepath, output_filepath)
        if isinstance(err, HTTPException):
            raise err
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred during clip generation: {str(err)}"
        )