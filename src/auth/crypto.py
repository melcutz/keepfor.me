import hashlib
import hmac
import secrets
import struct
from datetime import datetime, timedelta, timezone

_ITERATIONS = 100000


def _pbkdf2_hmac_sha256(password: bytes, salt: bytes, iterations: int) -> bytes:
    """Pure-Python PBKDF2-HMAC-SHA256 for runtimes without hashlib.pbkdf2_hmac
    (e.g. Cloudflare Python Workers / Pyodide)."""
    dklen = hashlib.sha256().digest_size
    out = b""
    block = 1
    while len(out) < dklen:
        u = hmac.new(password, salt + struct.pack(">I", block), hashlib.sha256).digest()
        t = bytearray(u)
        for _ in range(iterations - 1):
            u = hmac.new(password, u, hashlib.sha256).digest()
            for j in range(len(t)):
                t[j] ^= u[j]
        out += bytes(t)
        block += 1
    return out[:dklen]


def _derive_key(password: str, salt: str) -> bytes:
    pw = password.encode("utf-8")
    sb = salt.encode("utf-8")
    try:
        return hashlib.pbkdf2_hmac("sha256", pw, sb, _ITERATIONS)  # type: ignore[attr-defined]
    except AttributeError:
        return _pbkdf2_hmac_sha256(pw, sb, _ITERATIONS)


def hash_password(password: str, salt: str | None = None) -> str:
    """Hashes a password using PBKDF2-HMAC-SHA256 with 100,000 iterations."""
    if not salt:
        salt = secrets.token_hex(16)
    key = _derive_key(password, salt)
    return f"{salt}${key.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verifies a password against stored salt$hash string."""
    try:
        salt, key_hex = stored_hash.split("$", 1)
        expected = _derive_key(password, salt)
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
