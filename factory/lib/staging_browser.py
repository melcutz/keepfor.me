"""Playwright browser session helper for Keepfor Cloud staging QA."""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

DEFAULT_USER_AGENT = "keepfor-smoke/1 (+https://keepfor.me)"

FORBIDDEN_PRODUCTION_HOSTS: frozenset[str] = frozenset(
    {
        "app.keepfor.me",
        "ops.keepfor.me",
        "keepfor.me",
        "www.keepfor.me",
    }
)

PERSONA_EMAILS: dict[str, str] = {
    "pro": "qa+pro@staging.keepfor.me",
    "personal": "qa+personal@staging.keepfor.me",
    "trialing": "qa+trialing@staging.keepfor.me",
    "expired": "qa+expired@staging.keepfor.me",
    "smoke": "qa+smoke@staging.keepfor.me",
    "ops": "qa+ops@staging.keepfor.me",
}


def validate_staging_url(url: str) -> str:
    """Validate that a target URL points to staging and never production."""
    cleaned = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or not host:
        raise ValueError(f"Invalid staging URL: {url!r}")
    if host in FORBIDDEN_PRODUCTION_HOSTS:
        raise ValueError(f"Refusing production URL in staging_browser: {url!r}")
    if "staging" not in host and host not in {"localhost", "127.0.0.1"}:
        raise ValueError(f"Refusing non-staging host in staging_browser: {url!r}")
    return cleaned


def build_access_headers(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build Cloudflare Access service token headers from environment variables."""
    src = env if env is not None else os.environ
    headers: dict[str, str] = {}
    client_id = src.get("CF_ACCESS_CLIENT_ID", "").strip()
    client_secret = src.get("CF_ACCESS_CLIENT_SECRET", "").strip()
    if client_id:
        headers["CF-Access-Client-Id"] = client_id
    if client_secret:
        headers["CF-Access-Client-Secret"] = client_secret
    return headers


def resolve_persona_target(
    persona: str,
    *,
    tag: str = "qa",
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    """Return (target_base_url, email, auth_mode) for a requested QA persona."""
    src = env if env is not None else os.environ
    base_url = validate_staging_url(
        src.get("KEEPFOR_STAGING_BASE_URL", "https://staging.keepfor.me")
    )
    ops_base_url = validate_staging_url(
        src.get("KEEPFOR_STAGING_OPS_BASE_URL", "https://ops-staging.keepfor.me")
    )

    if persona == "ops":
        return ops_base_url, PERSONA_EMAILS["ops"], "ops_login"
    if persona == "anonymous":
        return base_url, "", "anonymous"
    if persona == "fresh_signup":
        safe_tag = re.sub(r"[^a-z0-9-]", "", (tag or "qa").lower())[:20] or "qa"
        email = f"qa+job-{safe_tag}-{secrets.token_hex(4)}@staging.keepfor.me"
        return base_url, email, "register"
    if persona in PERSONA_EMAILS:
        return base_url, PERSONA_EMAILS[persona], "user_login"
    raise ValueError(f"Unknown QA persona {persona!r}; expected one of {sorted(PERSONA_EMAILS)} or 'anonymous'/'fresh_signup'")


@dataclass
class StagingQASession:
    """Active Playwright staging browser session with automatic error capture."""

    page: Any
    context: Any
    base_url: str
    email: str
    password: str = ""
    artifacts_dir: str | None = None
    server_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)

    def screenshot(self, name: str, *, full_page: bool = True) -> str:
        """Capture a screenshot into artifacts_dir and return its path."""
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", name.strip()) or "screenshot"
        if not safe_name.lower().endswith(".png"):
            safe_name += ".png"
        out_dir = self.artifacts_dir or os.getcwd()
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, safe_name)
        if self.page is not None:
            self.page.screenshot(path=out_path, full_page=full_page)
        return out_path

    def assert_no_runtime_errors(self) -> None:
        """Raise AssertionError if any 5xx responses or uncaught JS errors occurred."""
        problems: list[str] = []
        if self.server_errors:
            problems.append("Server 5xx errors: " + "; ".join(self.server_errors[:10]))
        if self.page_errors:
            problems.append("Uncaught page errors: " + "; ".join(self.page_errors[:10]))
        if problems:
            raise AssertionError(" | ".join(problems))


@contextlib.contextmanager
def staging_session(
    persona: str = "pro",
    *,
    headless: bool = True,
    artifacts_dir: str | None = None,
    tag: str = "qa",
) -> Iterator[StagingQASession]:
    """Open an authenticated Playwright Chromium session against Keepfor staging."""
    from playwright.sync_api import sync_playwright

    target_url, email, auth_mode = resolve_persona_target(persona, tag=tag)
    headers = build_access_headers()
    qa_pw = os.environ.get("KEEPFOR_STAGING_QA_PASSWORD", "").strip()
    if auth_mode != "anonymous" and not qa_pw:
        raise RuntimeError("KEEPFOR_STAGING_QA_PASSWORD is not set in environment")

    effective_pw = qa_pw
    if auth_mode == "register" and len(effective_pw) < 12:
        effective_pw = f"{effective_pw}-StagingQA12!"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            base_url=target_url,
            user_agent=DEFAULT_USER_AGENT,
            extra_http_headers=headers,
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        session = StagingQASession(
            page=page,
            context=context,
            base_url=target_url,
            email=email,
            password=effective_pw if auth_mode != "anonymous" else "",
            artifacts_dir=artifacts_dir,
        )

        def _on_response(resp: Any) -> None:
            try:
                host = (urllib.parse.urlparse(resp.url).hostname or "").lower()
                if host.endswith("keepfor.me") and resp.status >= 500:
                    session.server_errors.append(
                        f"{resp.status} {resp.request.method} {resp.url}"
                    )
            except Exception:
                pass

        def _on_pageerror(exc: Any) -> None:
            session.page_errors.append(str(exc))

        def _on_console(msg: Any) -> None:
            try:
                if msg.type == "error":
                    session.console_errors.append(msg.text)
            except Exception:
                pass

        page.on("response", _on_response)
        page.on("pageerror", _on_pageerror)
        page.on("console", _on_console)

        try:
            if auth_mode == "user_login":
                page.goto(f"{target_url}/auth/login", wait_until="domcontentloaded", timeout=30000)
                page.locator('input[name="email"]').fill(email)
                page.locator('input[name="password"]').fill(effective_pw)
                page.locator('button[type="submit"]').click()
                page.wait_for_load_state("networkidle", timeout=30000)
                if urllib.parse.urlparse(page.url).path.rstrip("/") == "/auth/login":
                    raise RuntimeError(
                        f"Staging login failed for {email}; remained on {page.url}"
                    )
            elif auth_mode == "ops_login":
                page.goto(f"{target_url}/ops/login", wait_until="domcontentloaded", timeout=30000)
                page.locator('input[name="email"]').fill(email)
                page.locator('input[name="password"]').fill(effective_pw)
                page.locator('button[type="submit"]').click()
                page.wait_for_load_state("networkidle", timeout=30000)
                if urllib.parse.urlparse(page.url).path.rstrip("/") == "/ops/login":
                    raise RuntimeError(
                        f"Staging ops login failed for {email}; remained on {page.url}"
                    )
            elif auth_mode == "register":
                page.goto(f"{target_url}/auth/register", wait_until="domcontentloaded", timeout=30000)
                page.locator('input[name="email"]').fill(email)
                page.locator('input[name="password"]').fill(effective_pw)
                page.locator('button[type="submit"]').click()
                page.wait_for_load_state("networkidle", timeout=30000)
                if urllib.parse.urlparse(page.url).path.rstrip("/") == "/auth/register":
                    raise RuntimeError(
                        f"Staging registration failed for {email}; remained on {page.url}"
                    )

            yield session
        finally:
            context.close()
            browser.close()
