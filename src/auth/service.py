import uuid
from typing import Any

from src.auth.crypto import (
    generate_pat,
    generate_session_token,
    get_session_expiry,
    hash_password,
    hash_token,
    verify_password,
)
from src.models.db import Database
from src.utils.logging import logger


class AuthError(Exception):
    pass


class RegistrationClosedError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


async def register_user(
    db: Database, email: str, password: str, allow_public_signups: bool = False
) -> dict[str, Any]:
    email_clean = email.strip().lower()
    if not email_clean or len(password) < 8:
        raise AuthError("Valid email and minimum 8-character password are required.")

    count_row = await db.query_first("SELECT COUNT(*) as count FROM users;")
    user_count = count_row["count"] if count_row else 0

    if user_count > 0 and not allow_public_signups:
        logger.warning(f"Registration attempt blocked: {email_clean}")
        raise RegistrationClosedError("Registration is closed on this instance.")

    role = "admin" if user_count == 0 else "user"
    user_id = str(uuid.uuid4())
    pw_hash = hash_password(password)

    await db.execute(
        "INSERT INTO users (id, email, password_hash, role) VALUES (?, ?, ?, ?);",
        (user_id, email_clean, pw_hash, role),
    )
    logger.info(f"User registered: {user_id}, role: {role}")
    return {"id": user_id, "email": email_clean, "role": role}


async def login_user(
    db: Database, email: str, password: str
) -> tuple[dict[str, Any], str]:
    email_clean = email.strip().lower()
    user = await db.query_first("SELECT * FROM users WHERE email = ?;", (email_clean,))
    if not user or not verify_password(password, user["password_hash"]):
        logger.warning(f"Failed login attempt: {email_clean}")
        raise InvalidCredentialsError("Invalid email or password.")

    session_id = generate_session_token()
    expiry = get_session_expiry(days=30)

    await db.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?);",
        (session_id, user["id"], expiry),
    )
    logger.info(f"User logged in: {user['id']}")
    return {"id": user["id"], "email": user["email"], "role": user["role"]}, session_id


async def validate_session(db: Database, session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    row = await db.query_first(
        """
        SELECT u.id, u.email, u.role, s.expires_at
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.id = ? AND s.expires_at > CURRENT_TIMESTAMP;
        """,
        (session_id,),
    )
    if not row:
        return None
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


async def logout_session(db: Database, session_id: str) -> None:
    if session_id:
        await db.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))


async def create_pat(db: Database, user_id: str, name: str) -> dict[str, Any]:
    raw_token, token_hash = generate_pat()
    pat_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO personal_access_tokens (id, user_id, name, token_hash) "
        "VALUES (?, ?, ?, ?);",
        (pat_id, user_id, name.strip() or "Default PAT", token_hash),
    )
    return {"id": pat_id, "name": name, "token": raw_token}


async def validate_pat(db: Database, raw_token: str) -> dict[str, Any] | None:
    if not raw_token or not (
        raw_token.startswith("kfm_live_") or raw_token.startswith("rk_live_")
    ):
        return None
    token_hash = hash_token(raw_token)
    row = await db.query_first(
        """
        SELECT u.id, u.email, u.role, p.id as pat_id
        FROM personal_access_tokens p
        JOIN users u ON p.user_id = u.id
        WHERE p.token_hash = ? AND (p.expires_at IS NULL OR
                        p.expires_at > CURRENT_TIMESTAMP);
        """,
        (token_hash,),
    )
    if not row:
        return None

    # Update last_used_at asynchronously
    await db.execute(
        "UPDATE personal_access_tokens SET last_used_at = CURRENT_TIMESTAMP "
        "WHERE id = ?;",
        (row["pat_id"],),
    )
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


async def list_pats(db: Database, user_id: str) -> list[dict[str, Any]]:
    return await db.query_all(
        "SELECT id, name, last_used_at, created_at "
        "FROM personal_access_tokens WHERE user_id = ? "
        "ORDER BY created_at DESC;",
        (user_id,),
    )


async def delete_pat(db: Database, user_id: str, pat_id: str) -> None:
    await db.execute(
        "DELETE FROM personal_access_tokens WHERE id = ? AND user_id = ?;",
        (pat_id, user_id),
    )
