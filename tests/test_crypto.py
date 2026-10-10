# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for authentication cryptography utilities."""

from keepfor.auth.crypto import (
    generate_pat,
    generate_session_token,
    hash_password,
    hash_token,
    verify_password,
)


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
