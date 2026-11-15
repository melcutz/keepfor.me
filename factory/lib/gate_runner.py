"""Deterministic gate runner: protected_paths diff check + factory/checks.sh execution."""
from __future__ import annotations

import contextlib
import fnmatch
import glob
import os
import shutil
import subprocess
import sys
import threading
from typing import Iterator

from factory.lib.git_ops import (
    _run_git,
    changed_files_against_base,
    is_worktree_clean,
    reset_worktree_clean,
)

HARDCODED_PROTECTED: tuple[str, ...] = (
    "factory/checks.sh",
    "factory/config.json",
    "factory/bin/",
    "factory/lib/",
    ".agents/",
)

MAX_CHECKS_MD_LINES = 120

_FULL_GATE_LOCK = threading.Lock()
_ACTIVE_FULL_GATES = 0

_FULL_PRE_MERGE_INFRA_EXACT: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "src/keepfor_cloud/app.py",
        "src/keepfor_cloud/config.py",
        "src/keepfor_cloud/providers.py",
        "keepfor/app.py",
        "keepfor/config.py",
        "keepfor/models/db.py",
    }
)


def compute_pytest_workers(active_full_gates: int, cpu_count: int | None = None) -> int:
    """Compute bounded pytest-xdist worker count inversely proportional to concurrent full gates."""
    cpus = max(1, int(cpu_count or os.cpu_count() or 4))
    slots = max(1, int(active_full_gates))
    return min(8, max(2, cpus // slots))


@contextlib.contextmanager
def _full_gate_slot() -> Iterator[int]:
    global _ACTIVE_FULL_GATES
    with _FULL_GATE_LOCK:
        _ACTIVE_FULL_GATES += 1
        active = _ACTIVE_FULL_GATES
    try:
        yield active
    finally:
        with _FULL_GATE_LOCK:
            _ACTIVE_FULL_GATES = max(0, _ACTIVE_FULL_GATES - 1)


def select_pre_merge_gate_scope(
    upstream_changed_files: list[str],
    *,
    had_code_conflicts: bool,
) -> str:
    """Choose 'full' or 'slice' gate scope after merging base_branch into a PR worktree."""
    if had_code_conflicts:
        return "full"
    for raw in upstream_changed_files or ():
        norm = _normalize_relpath(raw)
        if not norm:
            continue
        if (
            norm in _FULL_PRE_MERGE_INFRA_EXACT
            or norm.startswith("vendor/")
            or norm.startswith("control_migrations/")
            or norm.endswith("/conftest.py")
        ):
            return "full"
    return "slice"


def _normalize_relpath(path: str) -> str:
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = os.path.normpath(cleaned).replace("\\", "/")
    if cleaned == ".":
        return ""
    return cleaned.lstrip("/")


def _matches_pattern(relpath: str, pattern: str) -> bool:
    pat = pattern.strip().replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    if not pat:
        return False
    if pat.endswith("/"):
        prefix = pat.rstrip("/")
        return relpath == prefix or relpath.startswith(prefix + "/")
    if any(ch in pat for ch in "*?["):
        return fnmatch.fnmatch(relpath, pat)
    return relpath == pat or relpath.startswith(pat + "/")


def find_protected_violations(changed_files: list[str], configured_patterns: list[str]) -> list[str]:
    patterns = list(HARDCODED_PROTECTED) + list(configured_patterns or [])
    violations: list[str] = []
    for raw in changed_files:
        norm = _normalize_relpath(raw)
        if not norm:
            continue
        if any(_matches_pattern(norm, pat) for pat in patterns):
            violations.append(norm)
    return sorted(set(violations))


def _repo_uses_ruff(worktree: str) -> bool:
    for cfg_name in ("ruff.toml", ".ruff.toml"):
        if os.path.isfile(os.path.join(worktree, cfg_name)):
            return True
    pyproject = os.path.join(worktree, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            with open(pyproject, encoding="utf-8") as fh:
                return "ruff" in fh.read()
        except OSError:
            return False
    return False


def _resolve_ruff_cmd(worktree: str) -> list[str] | None:
    if not _repo_uses_ruff(worktree):
        return None
    wt_ruff = os.path.join(worktree, ".venv", "bin", "ruff")
    if os.path.isfile(wt_ruff):
        return [wt_ruff]
    exe_ruff = os.path.join(os.path.dirname(sys.executable), "ruff")
    if os.path.isfile(exe_ruff):
        return [exe_ruff]
    which_ruff = shutil.which("ruff")
    if which_ruff:
        return [which_ruff]
    if os.path.isfile(os.path.join(worktree, "uv.lock")) and shutil.which("uv"):
        return ["uv", "run", "--frozen", "ruff"]
    return None


def auto_polish_worktree(
    worktree: str,
    base_branch: str,
    protected_paths: list[str] | None = None,
) -> bool:
    """Deterministically format changed Python files and rebuild stale Tailwind CSS before gate."""
    if not is_worktree_clean(worktree):
        return False

    prot = list(protected_paths or [])
    changed = [_normalize_relpath(p) for p in changed_files_against_base(worktree, base_branch)]
    changed = [p for p in changed if p]

    py_files = [
        p
        for p in changed
        if p.endswith(".py")
        and not p.startswith("vendor/")
        and not find_protected_violations([p], prot)
        and os.path.isfile(os.path.join(worktree, p))
    ]

    build_tw = os.path.join(worktree, "scripts", "build_tailwind.py")
    static_dir = os.path.join(worktree, "src", "keepfor_cloud", "static")
    existing_tw = glob.glob(os.path.join(static_dir, "tailwind-*.css")) if os.path.isdir(static_dir) else []
    tw_trigger = len(existing_tw) > 1 or any(
        p.startswith("templates/")
        or p.startswith("src/keepfor_cloud/")
        or p in {"tailwind.config.js", "tailwind-input.css"}
        for p in changed
    )
    needs_tailwind = os.path.isfile(build_tw) and tw_trigger

    if not py_files and not needs_tailwind:
        return False

    sub_env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        if py_files:
            ruff_cmd = _resolve_ruff_cmd(worktree)
            if ruff_cmd:
                fix_res = subprocess.run(
                    [*ruff_cmd, "check", "--fix-only", "--select=I,W", "--", *py_files],
                    cwd=worktree,
                    env=sub_env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if fix_res.returncode != 0:
                    reset_worktree_clean(worktree)
                    return False
                fmt_res = subprocess.run(
                    [*ruff_cmd, "format", "--", *py_files],
                    cwd=worktree,
                    env=sub_env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                if fmt_res.returncode != 0:
                    reset_worktree_clean(worktree)
                    return False

        if needs_tailwind:
            tw_res = subprocess.run(
                ["python3", "scripts/build_tailwind.py"],
                cwd=worktree,
                env=sub_env,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if tw_res.returncode != 0:
                reset_worktree_clean(worktree)
                return False

        if is_worktree_clean(worktree):
            return False

        _run_git(worktree, "add", "-A")
        staged_raw = _run_git(worktree, "diff", "--cached", "-z", "--name-only").stdout
        staged = [s for s in staged_raw.split("\0") if s]
        if not staged or find_protected_violations(staged, prot):
            reset_worktree_clean(worktree)
            return False

        _run_git(worktree, "commit", "-m", "chore(factory): auto-polish formatting and assets")
        return True
    except Exception:
        reset_worktree_clean(worktree)
        return False


def run_gate(
    repo_root: str,
    job_id: str,
    worktree: str,
    round_num: int,
    config: dict,
    tag: str | None = None,
    scope: str = "full",
) -> tuple[bool, str]:
    """Check protected_paths and execute main checkout's factory/checks.sh inside worktree."""
    factory_dir = os.path.join(repo_root, "factory")
    round_dir = os.path.join(factory_dir, "jobs", job_id, f"round-{round_num}")
    os.makedirs(round_dir, exist_ok=True)

    prefix = f"{tag}-" if tag else ""
    log_path = os.path.join(round_dir, f"{prefix}checks.log")
    md_path = os.path.join(round_dir, f"{prefix}checks.md")

    base_branch = config.get("base_branch", "main")
    changed = changed_files_against_base(worktree, base_branch)
    violations = find_protected_violations(changed, config.get("protected_paths", []))

    if violations:
        msg = "Protected path violation(s) detected on branch:\n" + "\n".join(f"- {v}" for v in violations) + "\n"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg)
        md = "\n".join(
            [
                "VERDICT: FAIL",
                "",
                "## Summary",
                "- Gate rejected modifications to protected paths.",
                "",
                "## Violations",
                *(f"- `{v}`" for v in violations),
                "",
            ]
        )
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        if tag:
            # Keep canonical round-level checks.md updated with latest gate result
            with open(os.path.join(round_dir, "checks.md"), "w", encoding="utf-8") as f:
                f.write(md)
        return False, md

    checks_script = os.path.join(factory_dir, "checks.sh")
    timeout_sec = int(config.get("gate_timeout_sec", 600))
    slot_cm = _full_gate_slot() if scope == "full" else contextlib.nullcontext(1)
    with slot_cm as active_gates:
        gate_env = {
            **os.environ,
            "FACTORY_GATE_SCOPE": scope,
            "FACTORY_BASE_BRANCH": str(base_branch),
        }
        if scope == "full":
            gate_env.setdefault("PYTEST_WORKERS", str(compute_pytest_workers(active_gates)))
        try:
            proc = subprocess.run(
                ["bash", checks_script],
                cwd=worktree,
                env=gate_env,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
            exit_code = proc.returncode
            full_output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            full_output = f"checks.sh timed out after {timeout_sec}s\n{exc.stdout or ''}\n{exc.stderr or ''}"

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(full_output)

    lines = full_output.splitlines()
    truncated = len(lines) > MAX_CHECKS_MD_LINES
    tail_text = "\n".join(lines[-MAX_CHECKS_MD_LINES:])
    verdict = "PASS" if exit_code == 0 else "FAIL"
    trunc_note = f" (showing last {MAX_CHECKS_MD_LINES} of {len(lines)} lines; full log in `{os.path.basename(log_path)}`)" if truncated else ""

    md = "\n".join(
        [
            f"VERDICT: {verdict}",
            "",
            "## Summary",
            f"- Ran `factory/checks.sh` in `{worktree}`",
            f"- Exit code: `{exit_code}`{trunc_note}",
            "",
            "## Output Tail",
            "```text",
            tail_text,
            "```",
            "",
        ]
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    if tag:
        with open(os.path.join(round_dir, "checks.md"), "w", encoding="utf-8") as f:
            f.write(md)
        with open(os.path.join(round_dir, "checks.log"), "w", encoding="utf-8") as f:
            f.write(full_output)

    return exit_code == 0, md
