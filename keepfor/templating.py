# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
"""Jinja2 templating environment and configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from keepfor.paths import templates_dir

_core_loader = FileSystemLoader(str(templates_dir()))
jinja_env = Environment(loader=ChoiceLoader([_core_loader]), autoescape=True)


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
