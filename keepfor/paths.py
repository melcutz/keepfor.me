# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from pathlib import Path

_PKG = Path(__file__).resolve().parent
_REPO = _PKG.parent


def _first_existing(*c: Path) -> Path:
    for p in c:
        if p.is_dir():
            return p
    raise FileNotFoundError(f"none of {[str(x) for x in c]} exist")


def templates_dir() -> Path:
    return _first_existing(_PKG / "templates", _REPO / "templates")


def static_dir() -> Path:
    return _first_existing(_PKG / "static", _REPO / "static")


def migrations_dir() -> Path:
    return _first_existing(_PKG / "migrations", _REPO / "migrations")
