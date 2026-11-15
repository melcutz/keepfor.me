#!/usr/bin/env bash
# factory/checks.sh -- deterministic verification gate run in every job worktree for keepfor.me.
# Mirrors .github/workflows/test.yml and AGENTS.md invariants without mutating tracked files.

set -euo pipefail

# Ensure uv resolves the current worktree's .venv rather than an inherited parent shell venv.
unset VIRTUAL_ENV

echo "==> [1/6] Ruff lint check"
uv run --extra dev ruff check keepfor/ tests/ --select=E,W,F,I,N

echo "==> [2/6] Ruff format check"
uv run --extra dev ruff format --check keepfor/ tests/

echo "==> [3/6] SQL tenant scoping audit"
sql_out="$(uv run --extra dev python3 scripts/audit_sql.py 2>&1)" || { echo "${sql_out}"; exit 1; }
echo "${sql_out}" | tail -n 4

echo "==> [4/6] Auth route order check"
uv run --extra dev python3 scripts/check_auth_order.py

echo "==> [5/6] Secret pattern scan"
python3 -c '
import subprocess
import sys

pattern = r"(sk_live_[A-Za-z0-9]{20,}|rk_live_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[bp]-[A-Za-z0-9]{10,}|cf_[A-Za-z0-9]{30,})"
res = subprocess.run(
    ["git", "grep", "--untracked", "-nE", pattern, "--", ".", ":(exclude).worktrees", ":(exclude)factory/jobs"],
    capture_output=True,
    text=True,
)
if res.returncode == 0 and res.stdout.strip():
    print(f"ERROR: Suspicious live secret pattern found:\n{res.stdout}", file=sys.stderr)
    sys.exit(1)
print("PASS: Secret scan clean.")
'

echo "==> [6/6] Pytest suite"
has_xdist=0
if uv run --extra dev python3 -c "import xdist" >/dev/null 2>&1; then
  has_xdist=1
fi
full_xdist_args=()
if [[ "${has_xdist}" -eq 1 ]]; then
  full_xdist_args=(-n "${PYTEST_WORKERS:-8}" --dist loadfile)
fi

if [[ "${FACTORY_GATE_SCOPE:-full}" == "slice" ]]; then
  mapfile -t scoped_tests < <(python3 -c '
import glob
import os
import subprocess

base = os.environ.get("FACTORY_BASE_BRANCH", "main")
diff_ref = f"origin/{base}...HEAD"
res = subprocess.run(["git", "diff", "--name-only", diff_ref], capture_output=True, text=True, check=False)
if res.returncode != 0:
    res = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True, check=False)
changed = [p.strip() for p in res.stdout.splitlines() if p.strip()]

targets: set[str] = set()
tokens: set[str] = set()
pkg_dirs: set[str] = set()
for path in changed:
    if path.startswith("tests/") and path.endswith(".py") and os.path.isfile(path):
        bname = os.path.basename(path)
        if bname.startswith("test_") or bname.endswith("_test.py"):
            targets.add(path)
    elif path.startswith("keepfor/") and path.endswith(".py"):
        parts = path[len("keepfor/"):].split("/")
        if len(parts) > 1 and os.path.isdir(os.path.join("tests", parts[0])):
            pkg_dirs.add(os.path.join("tests", parts[0]))
        for part in parts:
            stem = part[:-3] if part.endswith(".py") else part
            if stem and stem not in {"__init__", "routes", "models", "repo", "service", "utils"}:
                tokens.add(stem)

for pdir in sorted(pkg_dirs):
    for match in glob.glob(f"{pdir}/**/test_*.py", recursive=True):
        if os.path.isfile(match):
            targets.add(match)

for tok in sorted(tokens):
    for match in glob.glob(f"tests/**/test_*{tok}*.py", recursive=True):
        if os.path.isfile(match):
            targets.add(match)

for t in sorted(targets):
    print(t)
')
  if [[ ${#scoped_tests[@]} -gt 0 ]]; then
    slice_xdist_args=()
    if [[ "${has_xdist}" -eq 1 && ${#scoped_tests[@]} -gt 4 ]]; then
      slice_xdist_args=(-n "${PYTEST_WORKERS:-4}" --dist loadfile)
    fi
    echo "    (slice scope: running ${#scoped_tests[@]} targeted test file(s): ${scoped_tests[*]})"
    uv run --extra dev pytest -q -o addopts="" "${slice_xdist_args[@]}" "${scoped_tests[@]}"
  elif [[ -f tests/test_app_factory.py ]]; then
    echo "    (slice scope: no specific test files matched diff; running tests/test_app_factory.py)"
    uv run --extra dev pytest -q -o addopts="" tests/test_app_factory.py
  else
    uv run --extra dev pytest -q -o addopts="" "${full_xdist_args[@]}" tests/
  fi
else
  uv run --extra dev pytest -q -o addopts="" "${full_xdist_args[@]}" tests/
fi
