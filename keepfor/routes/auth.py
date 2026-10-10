# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from keepfor import deps
from keepfor.auth.service import (
    InvalidCredentialsError,
    RegistrationClosedError,
    login_user,
    logout_session,
    register_user,
)
from keepfor.routes._shared import _is_safe_path, _safe_next
from keepfor.schemas import LoginRequest, RegisterRequest
from keepfor.templating import jinja_env
from keepfor.utils.logging import logger
from keepfor.utils.rate_limit import (
    check_allowed,
    clear_account,
    rate_limited_html,
    record_failure,
)

router = APIRouter()

__all__ = ["router", "_is_safe_path", "_safe_next"]


@router.get("/auth/login", response_class=HTMLResponse)
async def login_get(
    request: Request, error: str | None = None, next: str | None = None
):
    # Signed-in visitors should never see the login form.
    if await deps.get_current_user(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    template = jinja_env.get_template("login.html")
    return HTMLResponse(content=template.render(error=error, next=next or ""))


@router.post("/auth/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    db = deps.get_db(request)
    # Throttle BEFORE hashing: PBKDF2 is deliberately expensive, so a blocked
    # client must not be allowed to make us do the work.
    limit = await check_allowed(db, request.headers, email)
    if not limit.allowed:
        return HTMLResponse(
            content=rate_limited_html(limit.retry_after),
            status_code=429,
            headers={"Retry-After": str(limit.retry_after)},
        )
    # Validate input with model
    try:
        validated = LoginRequest(email=email, password=password)
    except Exception as e:
        logger.warning(f"Login validation failed: {e}")
        template = jinja_env.get_template("login.html")
        return HTMLResponse(
            content=template.render(error="Invalid input", next=next),
            status_code=400,
        )

    try:
        user, session_id = await login_user(db, validated.email, validated.password)
        # A successful sign-in clears the account counter so a legitimate user
        # who mistyped a few times is never locked out.
        await clear_account(db, validated.email)
        resp = RedirectResponse(url=_safe_next(next), status_code=303)
        resp.set_cookie(
            key="kfm_session",
            value=session_id,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            secure=True,
        )
        return resp
    except InvalidCredentialsError as err:
        await record_failure(db, request.headers, email)
        template = jinja_env.get_template("login.html")
        return HTMLResponse(
            content=template.render(error=str(err), next=next), status_code=400
        )


@router.get("/auth/register", response_class=HTMLResponse)
async def register_get(
    request: Request, error: str | None = None, next: str | None = None
):
    # Registration is closed after the first user anyway; don't show the
    # form to someone already signed in.
    if await deps.get_current_user(request):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    template = jinja_env.get_template("register.html")
    return HTMLResponse(content=template.render(error=error, next=next or ""))


@router.post("/auth/register")
async def register_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    db = deps.get_db(request)
    env = deps.get_env_from_request(request)
    allow_signups = (
        getattr(env, "ALLOW_PUBLIC_SIGNUPS", "false") == "true" if env else False
    )

    # Same throttle as login: registration also hashes, and when public
    # signups are enabled it is an account-creation endpoint worth limiting.
    limit = await check_allowed(db, request.headers, email)
    if not limit.allowed:
        return HTMLResponse(
            content=rate_limited_html(limit.retry_after),
            status_code=429,
            headers={"Retry-After": str(limit.retry_after)},
        )

    # Validate input with model
    try:
        validated = RegisterRequest(email=email, password=password)
    except Exception as e:
        logger.warning(f"Register validation failed: {e}")
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error="Invalid input", next=next),
            status_code=400,
        )

    try:
        await register_user(
            db, validated.email, validated.password, allow_public_signups=allow_signups
        )
        # Automatically log in after registration
        _, session_id = await login_user(db, validated.email, validated.password)
        resp = RedirectResponse(url=_safe_next(next), status_code=303)
        resp.set_cookie(
            key="kfm_session",
            value=session_id,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            secure=True,
        )
        return resp
    except RegistrationClosedError as err:
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error=str(err), next=next), status_code=403
        )
    except Exception as err:
        # Includes InvalidCredentialsError from the auto-login below, but the
        # account now exists, so it is not a credential-guessing failure.
        template = jinja_env.get_template("register.html")
        return HTMLResponse(
            content=template.render(error=str(err), next=next), status_code=400
        )


@router.get("/auth/logout")
@router.post("/auth/logout")
async def logout_route(request: Request):
    db = deps.get_db(request)
    session_id = request.cookies.get("kfm_session") or request.cookies.get("rk_session")
    if session_id:
        await logout_session(db, session_id)
    resp = RedirectResponse(url="/auth/login", status_code=303)
    resp.delete_cookie("kfm_session")
    resp.delete_cookie("rk_session")
    return resp
