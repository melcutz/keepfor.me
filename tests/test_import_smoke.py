# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

import pkgutil
import subprocess
import sys

import pytest

import keepfor


def _get_keepfor_modules() -> list[str]:
    modules = [keepfor.__name__]
    for module_info in pkgutil.walk_packages(keepfor.__path__, keepfor.__name__ + "."):
        modules.append(module_info.name)
    return sorted(modules)


@pytest.mark.parametrize("modname", _get_keepfor_modules())
def test_import_module_in_clean_subprocess(modname: str) -> None:
    """Import every module in keepfor in a fresh subprocess."""
    # Special runtime checks:
    # If a module requires Pyodide or Workers specific runtime environments that
    # are absent in standard CPython without shims, skip with an explanation.
    # Currently all keepfor modules import cleanly in CPython.
    cmd = [sys.executable, "-c", f"import {modname}"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, (
        f"Failed to import '{modname}' in fresh process:\n{res.stderr}"
    )
