"""Tests for database and authentication services."""

import pytest
from src.auth.service import (
    register_user, login_user, validate_session, logout_session,
    create_pat, validate_pat, list_pats, delete_pat, RegistrationClosedError
)

@pytest.mark.asyncio
async def test_first_user_admin_claim_and_lockout(db):
    """Test that first user claims admin role and public registration is locked."""
    # 1. First user registers -> claims admin
    admin = await register_user(db, "admin@keepfor.me", "password123", allow_public_signups=False)
    assert admin["role"] == "admin"
    assert admin["email"] == "admin@keepfor.me"

    # 2. Second user attempts registration -> blocked
    with pytest.raises(RegistrationClosedError):
        await register_user(db, "intruder@keepfor.me", "password123", allow_public_signups=False)

    # 3. If allow_public_signups=True -> allowed as user
    user2 = await register_user(db, "friend@keepfor.me", "password123", allow_public_signups=True)
    assert user2["role"] == "user"

@pytest.mark.asyncio
async def test_session_lifecycle(db):
    """Test session creation, validation, and logout."""
    await register_user(db, "user@example.com", "mypassword123")
    user, session_id = await login_user(db, "user@example.com", "mypassword123")
    assert user["email"] == "user@example.com"

    # Validate session
    validated = await validate_session(db, session_id)
    assert validated is not None
    assert validated["id"] == user["id"]

    # Logout
    await logout_session(db, session_id)
    logged_out = await validate_session(db, session_id)
    assert logged_out is None

@pytest.mark.asyncio
async def test_pat_lifecycle(db):
    """Test PAT creation, validation, listing, and deletion."""
    user = await register_user(db, "patuser@example.com", "password123")
    pat_data = await create_pat(db, user["id"], "Claude Desktop")
    raw_token = pat_data["token"]
    assert raw_token.startswith("kfm_live_")

    # Validate PAT
    pat_user = await validate_pat(db, raw_token)
    assert pat_user is not None
    assert pat_user["id"] == user["id"]

    # List PATs
    pats = await list_pats(db, user["id"])
    assert len(pats) == 1
    assert pats[0]["name"] == "Claude Desktop"

    # Delete PAT
    await delete_pat(db, user["id"], pat_data["id"])
    deleted_user = await validate_pat(db, raw_token)
    assert deleted_user is None
