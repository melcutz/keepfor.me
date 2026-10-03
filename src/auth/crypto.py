import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta

def hash_password(password: str, salt: str | None = None) -> str:
    """Hashes a password using PBKDF2-HMAC-SHA256 with 100,000 iterations."""
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}${key.hex()}"

def verify_password(password: str, stored_hash: str) -> bool:
    """Verifies a password against stored salt$hash string."""
    try:
        salt, key_hex = stored_hash.split("$", 1)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return hmac.compare_digest(key_hex, expected.hex())
    except Exception:
        return False

def generate_session_token() -> str:
    """Generates a secure random 32-byte hex session ID."""
    return secrets.token_hex(32)

def generate_pat() -> tuple[str, str]:
    """Generates a Personal Access Token and its SHA-256 hash.
    Returns (raw_token, token_hash).
    """
    raw_token = f"kfm_live_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return raw_token, token_hash

def hash_token(raw_token: str) -> str:
    """Computes SHA-256 hash of a bearer PAT."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def get_session_expiry(days: int = 30) -> str:
    """Returns ISO UTC timestamp for session expiry."""
    expiry = datetime.now(timezone.utc) + timedelta(days=days)
    return expiry.strftime("%Y-%m-%d %H:%M:%S")
