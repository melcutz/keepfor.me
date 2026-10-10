# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for package paths and wheel packaging."""

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from keepfor.paths import _first_existing, migrations_dir, static_dir, templates_dir


def test_paths_helpers_resolve_directories():
    """Verify that path helpers resolve valid existing directories."""
    td = templates_dir()
    assert td.is_dir()
    assert (td / "base.html").is_file()

    sd = static_dir()
    assert sd.is_dir()

    md = migrations_dir()
    assert md.is_dir()
    assert (md / "0001_initial_schema.sql").is_file()


def test_first_existing_raises_on_missing(tmp_path: Path):
    """Verify _first_existing raises FileNotFoundError when no paths exist."""
    missing1 = tmp_path / "nonexistent1"
    missing2 = tmp_path / "nonexistent2"
    with pytest.raises(FileNotFoundError, match="none of .* exist"):
        _first_existing(missing1, missing2)


def test_wheel_contents():
    """Build a wheel with uv build --wheel and assert required contents."""
    if not shutil.which("uv"):
        pytest.skip("uv binary not found in PATH")

    repo_root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp_dir:
        res = subprocess.run(
            ["uv", "build", "--wheel", "-o", tmp_dir],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"uv build failed: {res.stderr}"

        wheels = list(Path(tmp_dir).glob("*.whl"))
        assert len(wheels) == 1, f"Expected 1 wheel, found {len(wheels)}"
        wheel_path = wheels[0]

        with zipfile.ZipFile(wheel_path, "r") as z:
            names = z.namelist()

            # Must contain core code and assets
            assert "keepfor/app.py" in names
            assert "keepfor/spi.py" in names
            assert "keepfor/defaults.py" in names
            assert "keepfor/runtime.py" in names
            assert "keepfor/templates/base.html" in names
            assert "keepfor/migrations/0001_initial_schema.sql" in names

            # Static directory must have at least one file
            static_files = [n for n in names if n.startswith("keepfor/static/")]
            assert len(static_files) >= 1

            # Must not contain tests or src
            assert not any(n.startswith("tests/") for n in names)
            assert not any(n.startswith("src/") for n in names)


def test_version_match():
    """Verify that pyproject.toml version matches keepfor.__version__."""
    import tomllib

    import keepfor

    repo_root = Path(__file__).resolve().parent.parent
    pyproject_path = repo_root / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    pyproject_version = data["project"]["version"]
    assert pyproject_version == keepfor.__version__
    assert keepfor.__version__ == "1.1.2"
    assert keepfor.CORE_API_VERSION == 1
