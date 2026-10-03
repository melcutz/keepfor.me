"""Tests for authentication cryptography utilities."""

import pytest
from src.auth.crypto import hash_password, verify_password, generate_pat, hash_token, generate_session_token

def test_password_hashing():
    """Test password hashing and verification."""
    pw = "supersecret123"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed)
    assert not verify_password("wrongpassword", hashed)

def test_pat_generation():
    """Test PAT generation and hashing."""
    raw, token_hash = generate_pat()
    assert raw.startswith("kfm_live_")
    assert hash_token(raw) == token_hash
    assert raw != token_hash

def test_session_token():
    """Test session token generation uniqueness and length."""
    tok1 = generate_session_token()
    tok2 = generate_session_token()
    assert tok1 != tok2
    assert len(tok1) == 64
