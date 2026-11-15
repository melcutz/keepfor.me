# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

"""Tests for Factory agy_runner idle background watchdog and config defaults."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

from factory.lib.agy_runner import run_agent
from factory.lib.config import load_config
from factory.lib.schemas import SCHEMAS


def _init_git_worktree(worktree: Path) -> None:
    worktree.mkdir()
    subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "Test"], check=True
    )
    (worktree / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "init"], check=True
    )


def test_factory_idle_bg_grace_default_allows_normal_pytest_runs(
    tmp_path: Path,
) -> None:
    empty_factory = tmp_path / "factory"
    empty_factory.mkdir()
    assert load_config(str(empty_factory))["idle_bg_grace_sec"] == 600

    repo_factory = Path(__file__).resolve().parents[1] / "factory"
    assert load_config(str(repo_factory))["idle_bg_grace_sec"] == 600


def test_run_agent_kills_setsid_zombie_background_child_and_retries(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "wt"
    _init_git_worktree(worktree)

    factory_dir = tmp_path / "factory"
    (factory_dir / "agents").mkdir(parents=True)
    (factory_dir / "agents" / "builder.md").write_text("Build role.", encoding="utf-8")
    job_dir = tmp_path / "job-901"
    job_dir.mkdir()

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(
        f"""#!/usr/bin/env python3
import json
import subprocess
import sys
import time
from pathlib import Path

state = Path({str(state_dir)!r})
count_file = state / "count.txt"
attempt = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(attempt))

if attempt == 1:
    child = subprocess.Popen(
        ["sleep", "6"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (state / "grandchild.pid").write_text(str(child.pid))
    sys.stderr.write("root agent idle; waiting up to 30m0s for 1 background task(s)\\n")
    sys.stderr.flush()
    time.sleep(6)
    sys.exit(0)

out = {{
    "status": "SUCCESS",
    "structured_output": {{
        "verdict": "DONE",
        "summary": ["Completed slice"],
        "changes": ["README.md"],
        "tests_added": [],
        "blocked_reason": "",
        "notes": "",
    }},
    "usage": {{"input_tokens": 100, "output_tokens": 20}},
    "duration_seconds": 0.2,
}}
print(json.dumps(out))
""",
        encoding="utf-8",
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    env_override = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    validated, _, _ = run_agent(
        str(factory_dir),
        str(job_dir),
        str(worktree),
        "build",
        "builder",
        "gemini-4-hydrogen-medium",
        "Implement feature",
        SCHEMAS["build"],
        timeout_sec=30,
        sandbox=False,
        extra_env=env_override,
        idle_bg_grace_sec=0.4,
    )
    assert validated["verdict"] == "DONE"

    grandchild_pid = int((state_dir / "grandchild.pid").read_text().strip())
    deadline = time.monotonic() + 0.5
    alive = True
    try:
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
                break
        assert not alive, f"Grandchild PID {grandchild_pid} leaked"
    finally:
        try:
            os.killpg(grandchild_pid, 9)
        except OSError:
            pass


def test_run_agent_unlatches_idle_watchdog_after_background_task_finishes(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "wt"
    _init_git_worktree(worktree)

    factory_dir = tmp_path / "factory"
    (factory_dir / "agents").mkdir(parents=True)
    (factory_dir / "agents" / "builder.md").write_text("Build role.", encoding="utf-8")
    job_dir = tmp_path / "job-902"
    job_dir.mkdir()

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_agy = bin_dir / "agy"
    fake_agy.write_text(
        f"""#!/usr/bin/env python3
import json
import subprocess
import sys
import time
from pathlib import Path

state = Path({str(state_dir)!r})
count_file = state / "count.txt"
attempt = int(count_file.read_text()) + 1 if count_file.exists() else 1
count_file.write_text(str(attempt))

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(0.12)"],
    start_new_session=True,
)
sys.stderr.write("root agent idle; waiting up to 30m0s for 1 background task(s)\\n")
sys.stderr.flush()
child.wait()
# Simulate root agent resuming silently on stderr and thinking past grace.
time.sleep(0.55)

out = {{
    "status": "SUCCESS",
    "structured_output": {{
        "verdict": "DONE",
        "summary": ["Completed slice"],
        "changes": ["README.md"],
        "tests_added": [],
        "blocked_reason": "",
        "notes": "",
    }},
    "usage": {{"input_tokens": 100, "output_tokens": 20}},
    "duration_seconds": 0.7,
}}
print(json.dumps(out))
""",
        encoding="utf-8",
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)

    env_override = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    validated, _, _ = run_agent(
        str(factory_dir),
        str(job_dir),
        str(worktree),
        "build",
        "builder",
        "gemini-4-hydrogen-medium",
        "Implement feature",
        SCHEMAS["build"],
        timeout_sec=30,
        sandbox=False,
        extra_env=env_override,
        idle_bg_grace_sec=0.35,
    )
    assert validated["verdict"] == "DONE"
    assert int((state_dir / "count.txt").read_text().strip()) == 1
