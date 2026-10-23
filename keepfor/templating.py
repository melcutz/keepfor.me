# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Jinja2 templating environment and configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from keepfor.models.cleanup import BLOCKED_FETCH_COPY, classify_failure
from keepfor.paths import templates_dir
from keepfor.utils.dates import normalize_published_date
from keepfor.utils.favicon import favicon_url

_core_loader = FileSystemLoader(str(templates_dir()))
jinja_env = Environment(loader=ChoiceLoader([_core_loader]), autoescape=True)
# Templates show the published date through the same normaliser the
# extractors use, so a raw timestamp never reaches the page.
jinja_env.filters["published_date"] = normalize_published_date
jinja_env.filters["classify_failure"] = classify_failure
# Cards ask us for the favicon of a saved link: same-origin, never a 404.
jinja_env.globals["favicon_url"] = favicon_url
jinja_env.globals["BLOCKED_FETCH_COPY"] = BLOCKED_FETCH_COPY


def configure_templates(
    extra_dirs: Sequence[Path] = (),
    globals: Mapping[str, Any] | None = None,
) -> None:
    """Configure template loaders and global variables.

    extra_dirs are searched before core templates, allowing overrides.
    """
    loaders = [FileSystemLoader(str(d)) for d in extra_dirs] + [_core_loader]
    jinja_env.loader = ChoiceLoader(loaders)
    if globals:
        jinja_env.globals.update(globals)
