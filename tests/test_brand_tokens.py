# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Brand guard: the core UI uses the brand blue tokens, never green.

The token values match keepfor-cloud's ``keepfor_cloud/brand.py``; the cloud
injects its own copy of them, so the core only has to declare the defaults.
"""

from __future__ import annotations

import colorsys
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_ROOTS = ("templates", "static", "keepfor", "browser-extension")
SCAN_SUFFIXES = (".html", ".css", ".js")
SKIP_DIRS = {
    ".git",
    ".venv",
    ".venv-workers",
    "vendor",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    "python_modules",
}
# Minified third-party bundles, listed by filename (none vendored today).
SKIP_FILES: frozenset[str] = frozenset()

GREEN_CLASS = re.compile(r"\b(?:green|emerald|lime|teal)-\d{2,3}\b")
HEX_COLOR = re.compile(r"#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})\b", re.IGNORECASE)
BRAND_BLUE_CLASS = re.compile(
    r"\b(?:bg|text|border|ring|from|to|via|decoration|fill|stroke|divide|outline"
    r"|shadow|placeholder)-blue-\d{2,3}\b"
)
ALLOWED_HEX = {"#34a853"}  # Google's logo green

BRAND_TOKENS = {
    "--brand-50": "#eff6ff",
    "--brand-100": "#dbeafe",
    "--brand-200": "#bfdbfe",
    "--brand-300": "#93c5fd",
    "--brand-400": "#60a5fa",
    "--brand-500": "#3b82f6",
    "--brand-600": "#2563eb",
    "--brand-700": "#1d4ed8",
    "--brand-800": "#1e40af",
    "--brand-900": "#1e3a8a",
    "--brand-950": "#172554",
    "--brand-accent": "#2563eb",
}
DARK_ACCENT = "#60a5fa"


def _scanned_files() -> list[Path]:
    """Every HTML/CSS/JS file the brand rules apply to."""
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name in SKIP_FILES:
                continue
            files.append(path)
    return sorted(files)


def _is_green(value: str) -> bool:
    """True when the hex colour sits in the green hue range."""
    raw = value[1:]
    if len(raw) in (3, 4):
        raw = "".join(char * 2 for char in raw)
    if len(raw) == 8:
        raw = raw[:6]
    red, green, blue = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
    return 80 <= hue * 360 <= 170 and saturation >= 0.2


def test_no_green_classes_or_hex():
    """No green Tailwind class and no green hex anywhere in the scanned files."""
    problems: list[str] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        relative = path.relative_to(REPO_ROOT)
        for match in GREEN_CLASS.finditer(text):
            problems.append(f"{relative}: green class {match.group(0)}")
        for match in HEX_COLOR.finditer(text):
            value = match.group(0).lower()
            if value in ALLOWED_HEX:
                continue
            if _is_green(value):
                problems.append(f"{relative}: green hex {match.group(0)}")
    assert not problems, "\n".join(problems)


def test_templates_do_not_hardcode_blue_classes():
    """Templates use the brand tokens, not hard-coded blue Tailwind colours."""
    problems: list[str] = []
    for path in sorted((REPO_ROOT / "templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in BRAND_BLUE_CLASS.finditer(text):
            problems.append(
                f"{path.relative_to(REPO_ROOT)}: hard-coded {match.group(0)}"
            )
    assert not problems, "\n".join(problems)


def test_core_base_declares_brand_tokens():
    """base.html declares every brand token with the shared values."""
    text = (REPO_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\}", text, re.DOTALL)
    assert root, "base.html declares no :root block"
    for name, value in BRAND_TOKENS.items():
        assert re.search(
            rf"{re.escape(name)}\s*:\s*{re.escape(value)}\s*;", root.group(1)
        ), f"{name} is not declared with {value}"
    dark = re.search(r"body\.theme-dark\s*\{(.*?)\}", text, re.DOTALL)
    assert dark, "base.html declares no body.theme-dark block"
    assert re.search(
        rf"--brand-accent\s*:\s*{re.escape(DARK_ACCENT)}\s*;", dark.group(1)
    ), f"body.theme-dark does not set --brand-accent to {DARK_ACCENT}"
