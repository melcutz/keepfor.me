"""Staging deploy, secret isolation, QA account seeding, and rollback helpers."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from factory.lib.git_ops import _run_git, head_sha, repo_lock

STAGING_WEB_WORKER = "keepfor-cloud-web-staging"
STAGING_INGEST_WORKER = "keepfor-cloud-ingest-staging"
STAGING_CONTROL_DB = "keepfor-staging-control-db"
STAGING_OPS_EMAIL = "qa+ops@staging.keepfor.me"
SEEDED_QA_USER_EMAILS: tuple[str, ...] = (
    "qa+pro@staging.keepfor.me",
    "qa+personal@staging.keepfor.me",
    "qa+trialing@staging.keepfor.me",
    "qa+expired@staging.keepfor.me",
    "qa+smoke@staging.keepfor.me",
)
DEFAULT_USER_AGENT = "keepfor-smoke/1 (+https://keepfor.me)"

ALLOWED_STAGING_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_API_TOKEN_STAGING",
        "CLOUDFLARE_ACCOUNT_ID",
        "CF_ACCESS_CLIENT_ID",
        "CF_ACCESS_CLIENT_SECRET",
        "KEEPFOR_SMOKE_PAT",
        "QA_SIGNUP_ALLOWLIST",
        "QA_KF_PW",
        "KEEPFOR_STAGING_QA_PASSWORD",
        "KEEPFOR_STRIPE_TEST_KEY",
        "STRIPE_SECRET_KEY",
    }
)

FORBIDDEN_PROD_MARKERS: tuple[str, ...] = (
    "keepfor-cloud-web",
    "keepfor-cloud-ingest",
    "keepfor-cloud-control-db",
    "app.keepfor.me",
    "ops.keepfor.me",
)

_STAGING_THREAD_LOCK = threading.RLock()
_ACCOUNTS_SEEDED = False


@contextlib.contextmanager
def staging_lock(jobs_dir: str) -> Iterator[None]:
    """Serialize merging + staging_qa across threads and OS processes."""
    os.makedirs(jobs_dir, exist_ok=True)
    lock_path = os.path.join(jobs_dir, ".staging.lock")
    with _STAGING_THREAD_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _parse_env_value(raw_val: str) -> str:
    val = raw_val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
        quote = val[0]
        inner = val[1:-1]
        if quote == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return re.split(r"\s+#", val, maxsplit=1)[0].strip()


def load_staging_secrets(env_files: list[str]) -> dict[str, str]:
    """Parse allowlisted Keepfor staging credentials from env files without a shell."""
    secrets: dict[str, str] = {}
    for path in env_files or ():
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            continue
        with open(expanded, encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, raw_val = line.split("=", 1)
                key = key.strip()
                if key not in ALLOWED_STAGING_SECRET_KEYS:
                    continue
                val = _parse_env_value(raw_val)
                if key in {"STRIPE_SECRET_KEY", "KEEPFOR_STRIPE_TEST_KEY"} and (
                    val.startswith("sk_live_") or val.startswith("rk_live_")
                ):
                    raise ValueError(f"Refusing live Stripe key in {key}")
                if val:
                    secrets[key] = val

    if "KEEPFOR_STAGING_QA_PASSWORD" not in secrets and secrets.get("QA_KF_PW"):
        secrets["KEEPFOR_STAGING_QA_PASSWORD"] = secrets["QA_KF_PW"]
    if "CLOUDFLARE_API_TOKEN" not in secrets and secrets.get("CLOUDFLARE_API_TOKEN_STAGING"):
        secrets["CLOUDFLARE_API_TOKEN"] = secrets["CLOUDFLARE_API_TOKEN_STAGING"]
    secrets.setdefault("QA_SIGNUP_ALLOWLIST", "qa+*@staging.keepfor.me")
    return secrets


def build_deploy_env(
    secrets: dict[str, str],
    staging_cfg: dict,
    repo_root: str | None = None,
) -> dict[str, str]:
    """Build subprocess env for deterministic staging deploy, seed, and rollback."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    root = repo_root or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    venv_py = os.path.join(root, ".venv", "bin", "python")
    if os.path.isfile(venv_py):
        env.setdefault("PYTHON_BIN", venv_py)
    env["CI"] = "1"
    env.setdefault("WRANGLER", "npx --yes wrangler@4.147.0")
    for key in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "CF_ACCESS_CLIENT_ID",
        "CF_ACCESS_CLIENT_SECRET",
        "KEEPFOR_SMOKE_PAT",
        "QA_SIGNUP_ALLOWLIST",
        "KEEPFOR_STAGING_QA_PASSWORD",
    ):
        if secrets.get(key):
            env[key] = secrets[key]
    env["KEEPFOR_SMOKE_BASE_URL"] = staging_cfg.get("base_url", "https://staging.keepfor.me")
    return env


def build_qa_agent_env(secrets: dict[str, str], staging_cfg: dict) -> dict[str, str]:
    """Build least-privilege env for the Playwright QA agent (never includes CF API token)."""
    qa_env: dict[str, str] = {
        "KEEPFOR_STAGING_BASE_URL": staging_cfg.get("base_url", "https://staging.keepfor.me"),
        "KEEPFOR_STAGING_OPS_BASE_URL": staging_cfg.get(
            "ops_base_url", "https://ops-staging.keepfor.me"
        ),
    }
    for key in (
        "CF_ACCESS_CLIENT_ID",
        "CF_ACCESS_CLIENT_SECRET",
        "KEEPFOR_STAGING_QA_PASSWORD",
        "KEEPFOR_SMOKE_PAT",
        "QA_SIGNUP_ALLOWLIST",
        "KEEPFOR_STRIPE_TEST_KEY",
    ):
        if secrets.get(key):
            qa_env[key] = secrets[key]
    return qa_env


def redact_secrets(text: str, secrets: dict[str, str]) -> str:
    """Replace any loaded secret values (>= 6 chars) with [REDACTED]."""
    out = text or ""
    sensitive_values = sorted(
        {
            v
            for k, v in (secrets or {}).items()
            if v and len(v) >= 6 and k != "QA_SIGNUP_ALLOWLIST"
        },
        key=len,
        reverse=True,
    )
    for secret in sensitive_values:
        out = out.replace(secret, "[REDACTED]")
    return out


def ensure_staging_deploy_worktree(repo_root: str, base_branch: str) -> tuple[str, str]:
    """Maintain .worktrees/_staging-deploy detached at exact origin/<base_branch>."""
    wt_root = os.path.join(repo_root, ".worktrees")
    deploy_wt = os.path.join(wt_root, "_staging-deploy")
    with repo_lock(repo_root):
        _run_git(repo_root, "worktree", "prune", check=False)
        _run_git(repo_root, "fetch", "origin", base_branch)
        os.makedirs(wt_root, exist_ok=True)
        if not os.path.exists(os.path.join(deploy_wt, ".git")):
            _run_git(
                repo_root,
                "worktree",
                "add",
                "--detach",
                deploy_wt,
                f"origin/{base_branch}",
            )
        else:
            _run_git(deploy_wt, "fetch", "origin", base_branch)
            _run_git(deploy_wt, "checkout", "--detach", f"origin/{base_branch}")
            _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}")
            _run_git(deploy_wt, "clean", "-fd")
        return deploy_wt, head_sha(deploy_wt)


@dataclass(frozen=True)
class DeployPlan:
    """Selective Cloudflare staging deployment plan derived from git diff."""

    skip_non_runtime: bool
    extra_args: list[str]
    reason: str


@dataclass
class TrainOutcome:
    """Outcome delivered to a worker thread waiting on a coalesced staging train."""

    action: str  # "return" | "continue"
    job: dict | None = None
    error: BaseException | None = None


@dataclass
class TrainTicket:
    """Per-job ticket submitted to StagingTrainCoordinator."""

    job_id: str
    issue_number: int
    jdir: str
    worktree: str
    branch: str
    issue_md: str
    initial_stage: str
    pre_train_head_sha: str = ""
    merged_sha: str = ""
    pre_merge_sha: str = ""
    done_event: threading.Event = field(default_factory=threading.Event)
    outcome: TrainOutcome | None = None


class StagingTrainCoordinator:
    """Thread-safe Leader/Followers coordinator for coalesced merge + staging QA trains."""

    def __init__(
        self,
        max_batch_size: int = 4,
        coalesce_window_sec: float = 15.0,
        has_near_merge_peers: Callable[[set[str]], bool] | None = None,
    ) -> None:
        if isinstance(max_batch_size, bool) or int(max_batch_size) < 1:
            raise ValueError("max_batch_size must be an integer >= 1")
        if isinstance(coalesce_window_sec, bool) or float(coalesce_window_sec) < 0:
            raise ValueError("coalesce_window_sec must be >= 0")
        self.max_batch_size = int(max_batch_size)
        self.coalesce_window_sec = float(coalesce_window_sec)
        self._has_near_merge_peers = has_near_merge_peers
        self._cond = threading.Condition()
        self._pending: list[TrainTicket] = []
        self._leader_active = False

    def run_ticket(
        self,
        ticket: TrainTicket,
        execute_batch_fn: Callable[[list[TrainTicket]], None],
    ) -> TrainOutcome:
        """Queue ticket, coalesce up to max_batch_size, and wait for TrainOutcome."""
        with self._cond:
            self._pending.append(ticket)
            self._cond.notify_all()

        while not ticket.done_event.is_set():
            with self._cond:
                while not ticket.done_event.is_set() and (
                    self._leader_active
                    or (self._pending and self._pending[0] is not ticket)
                ):
                    self._cond.wait()

                if ticket.done_event.is_set():
                    break

                self._leader_active = True
                if self.max_batch_size > 1 and self.coalesce_window_sec > 0:
                    deadline = time.monotonic() + self.coalesce_window_sec
                    while len(self._pending) < self.max_batch_size:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        if self._has_near_merge_peers is not None:
                            queued_ids = {t.job_id for t in self._pending}
                            if not self._has_near_merge_peers(queued_ids):
                                break
                            self._cond.wait(timeout=min(0.2, remaining))
                        else:
                            self._cond.wait(timeout=remaining)

                batch = self._pending[: self.max_batch_size]
                del self._pending[: len(batch)]

            try:
                execute_batch_fn(batch)
            except BaseException as exc:
                for item in batch:
                    if not item.done_event.is_set():
                        item.outcome = TrainOutcome(action="return", error=exc)
                        item.done_event.set()
            finally:
                for item in batch:
                    if not item.done_event.is_set():
                        item.outcome = TrainOutcome(
                            action="return",
                            error=RuntimeError(
                                f"Train batch completed without outcome for {item.job_id}"
                            ),
                        )
                        item.done_event.set()
                with self._cond:
                    self._leader_active = False
                    self._cond.notify_all()

        assert ticket.outcome is not None
        if ticket.outcome.error is not None:
            raise ticket.outcome.error
        return ticket.outcome


def _is_non_runtime_path(path: str) -> bool:
    if path in {"pyproject.toml", "uv.lock"} or path.startswith("tailwind"):
        return False
    if path.startswith(
        (
            "src/",
            "apps/",
            "templates/",
            "control_migrations/",
            "scripts/",
            "vendor/",
        )
    ):
        return False
    return (
        path.startswith(("docs/", "tests/"))
        or path.endswith(".md")
        or path in {"LICENSE", ".gitignore"}
    )


def plan_staging_deploy(changed_files: list[str]) -> DeployPlan:
    """Compute selective deploy_staging.sh flags or non-runtime skip from changed files."""
    paths = [p.strip().lstrip("./") for p in (changed_files or []) if p and p.strip()]
    if not paths:
        return DeployPlan(
            skip_non_runtime=False,
            extra_args=["--web-only", "--skip-migrations"],
            reason="default_safe",
        )

    if all(_is_non_runtime_path(p) for p in paths):
        return DeployPlan(
            skip_non_runtime=True,
            extra_args=[],
            reason="non_runtime_only",
        )

    needs_ingest = any(
        p.startswith(("apps/ingest/", "src/keepfor_cloud/ai/", "vendor/"))
        or p
        in {
            "src/keepfor_cloud/ingest.py",
            "src/keepfor_cloud/providers.py",
            "src/keepfor_cloud/config.py",
            "pyproject.toml",
            "uv.lock",
            "scripts/deploy_staging.sh",
        }
        for p in paths
    )
    needs_migrations = any(
        p.startswith(("control_migrations/", "vendor/"))
        or p == "scripts/deploy_staging.sh"
        for p in paths
    )

    extra_args: list[str] = []
    if not needs_ingest:
        extra_args.append("--web-only")
    if not needs_migrations:
        extra_args.append("--skip-migrations")
    return DeployPlan(
        skip_non_runtime=False,
        extra_args=extra_args,
        reason="runtime_changes",
    )


def revert_commit_on_base(deploy_wt: str, base_branch: str, merged_sha: str) -> str:
    """Revert merged_sha on origin/<base_branch> (handling merge and single-parent commits)."""
    _run_git(deploy_wt, "fetch", "origin", base_branch)
    _run_git(deploy_wt, "checkout", "--detach", f"origin/{base_branch}")
    _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}")
    _run_git(deploy_wt, "clean", "-fd")

    rev_line = _run_git(deploy_wt, "rev-list", "--parents", "-n", "1", merged_sha).stdout.strip()
    tokens = rev_line.split()
    parent_count = max(0, len(tokens) - 1)
    if parent_count >= 2:
        _run_git(deploy_wt, "revert", "--no-edit", "-m", "1", merged_sha)
    else:
        _run_git(deploy_wt, "revert", "--no-edit", merged_sha)

    revert_sha = head_sha(deploy_wt)
    _run_git(deploy_wt, "push", "origin", f"HEAD:{base_branch}")
    return revert_sha


def try_revert_commits_on_base(
    deploy_wt: str,
    base_branch: str,
    merged_shas: Sequence[str],
) -> tuple[bool, dict[str, str], str]:
    """Attempt local git revert of merged_shas in order and push once if conflict-free."""
    shas = [s.strip() for s in merged_shas if s and s.strip()]
    if not shas:
        return True, {}, ""

    _run_git(deploy_wt, "fetch", "origin", base_branch)
    _run_git(deploy_wt, "checkout", "--detach", f"origin/{base_branch}")
    _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}")
    _run_git(deploy_wt, "clean", "-fd")

    revert_map: dict[str, str] = {}
    for sha in shas:
        rev_line = _run_git(deploy_wt, "rev-list", "--parents", "-n", "1", sha).stdout.strip()
        parent_count = max(0, len(rev_line.split()) - 1)
        cmd = (
            ["revert", "--no-edit", "-m", "1", sha]
            if parent_count >= 2
            else ["revert", "--no-edit", sha]
        )
        res = _run_git(deploy_wt, *cmd, check=False)
        if res.returncode != 0:
            detail = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
            _run_git(deploy_wt, "revert", "--abort", check=False)
            _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}", check=False)
            _run_git(deploy_wt, "clean", "-fd", check=False)
            return False, {}, detail
        revert_map[sha] = head_sha(deploy_wt)

    push_res = _run_git(deploy_wt, "push", "origin", f"HEAD:{base_branch}", check=False)
    if push_res.returncode != 0:
        detail = ((push_res.stdout or "") + "\n" + (push_res.stderr or "")).strip()
        _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}", check=False)
        _run_git(deploy_wt, "clean", "-fd", check=False)
        return False, {}, detail

    return True, revert_map, ""


def _restore_pr_branch_after_revert(
    repo_root: str,
    worktree: str,
    new_branch: str,
    base_branch: str,
    revert_sha: str,
    pre_train_head_sha: str = "",
) -> str:
    with repo_lock(repo_root):
        _run_git(worktree, "fetch", "origin", base_branch)
        _run_git(worktree, "checkout", "--", ".", check=False)
        _run_git(worktree, "clean", "-fd", check=False)
        _run_git(worktree, "checkout", "-B", new_branch, f"origin/{base_branch}")
        rev = _run_git(worktree, "revert", "--no-edit", revert_sha, check=False)
        if rev.returncode != 0:
            _run_git(worktree, "revert", "--abort", check=False)
            if not pre_train_head_sha:
                raise subprocess.CalledProcessError(
                    rev.returncode,
                    rev.args,
                    output=rev.stdout,
                    stderr=rev.stderr,
                )
            _run_git(worktree, "checkout", "-B", new_branch, pre_train_head_sha)
            _run_git(worktree, "merge", "--no-edit", f"origin/{base_branch}")
    return new_branch


def prepare_qa_fix_branch(
    repo_root: str,
    worktree: str,
    job_id: str,
    next_qa_round: int,
    base_branch: str,
    revert_sha: str,
    pre_train_head_sha: str = "",
) -> str:
    """Create factory/<job_id>-qa<N> off reverted base with the PR's code restored."""
    new_branch = f"factory/{job_id}-qa{int(next_qa_round)}"
    return _restore_pr_branch_after_revert(
        repo_root,
        worktree,
        new_branch,
        base_branch,
        revert_sha,
        pre_train_head_sha=pre_train_head_sha,
    )


def prepare_requeue_branch(
    repo_root: str,
    worktree: str,
    job_id: str,
    requeue_num: int,
    base_branch: str,
    revert_sha: str,
    pre_train_head_sha: str = "",
) -> str:
    """Create factory/<job_id>-requeue<N> off reverted base for an innocent passing PR."""
    new_branch = f"factory/{job_id}-requeue{int(requeue_num)}"
    return _restore_pr_branch_after_revert(
        repo_root,
        worktree,
        new_branch,
        base_branch,
        revert_sha,
        pre_train_head_sha=pre_train_head_sha,
    )


def get_worker_active_version(cwd: str, worker_name: str, deploy_env: dict[str, str]) -> str:
    """Return active Cloudflare Worker version_id for a staging worker."""
    if worker_name not in {STAGING_WEB_WORKER, STAGING_INGEST_WORKER}:
        raise ValueError(f"Refusing non-staging worker {worker_name!r}")
    proc = subprocess.run(
        ["npx", "--yes", "wrangler@4.147.0", "deployments", "list", "--name", worker_name, "--json"],
        cwd=cwd,
        env=deploy_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return ""
    try:
        items = json.loads(proc.stdout)
        if isinstance(items, list) and items:
            versions = items[-1].get("versions") or []
            if versions and isinstance(versions[0], dict):
                return str(versions[0].get("version_id") or "").strip()
    except (ValueError, KeyError, IndexError):
        return ""
    return ""


def ensure_staging_qa_accounts(
    deploy_wt: str,
    deploy_env: dict[str, str],
    *,
    force: bool = False,
) -> None:
    """Idempotently seed QA user accounts and qa+ops operator in staging D1."""
    global _ACCOUNTS_SEEDED
    if _ACCOUNTS_SEEDED and not force:
        return
    qa_pw = deploy_env.get("KEEPFOR_STAGING_QA_PASSWORD", "").strip()
    if not qa_pw:
        raise RuntimeError("KEEPFOR_STAGING_QA_PASSWORD is required to seed staging QA accounts")

    py_bin = deploy_env.get("PYTHON_BIN", "")
    py_cmd = [py_bin] if py_bin and os.path.isfile(py_bin) else ["uv", "run", "--frozen", "python"]
    sub_env = dict(deploy_env)
    src_dir = os.path.join(deploy_wt, "src")
    sub_env["PYTHONPATH"] = (
        f"{src_dir}:{sub_env['PYTHONPATH']}" if sub_env.get("PYTHONPATH") else src_dir
    )

    seed_proc = subprocess.run(
        [*py_cmd, "scripts/seed_staging.py", "--remote", "--apply"],
        cwd=deploy_wt,
        env=sub_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if seed_proc.returncode != 0:
        detail = (seed_proc.stderr or seed_proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"seed_staging.py failed (exit {seed_proc.returncode}): {detail}")

    hash_proc = subprocess.run(
        [
            *py_cmd,
            "-c",
            "import os; from keepfor.auth.crypto import hash_password; "
            "print(hash_password(os.environ['KEEPFOR_STAGING_QA_PASSWORD']))",
        ],
        cwd=deploy_wt,
        env=sub_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    pw_hash = hash_proc.stdout.strip().splitlines()[-1].strip()
    safe_hash = pw_hash.replace("'", "''")
    email_list = ", ".join(f"'{e}'" for e in SEEDED_QA_USER_EMAILS)
    sql = (
        f"UPDATE users SET password_hash = '{safe_hash}', "
        "email_verified_at = COALESCE(email_verified_at, '2026-11-01T00:00:00+00:00') "
        f"WHERE email IN ({email_list}); "
        "INSERT INTO operators (id, email, password_hash) "
        f"VALUES ('op_staging_qa_ops', '{STAGING_OPS_EMAIL}', '{safe_hash}') "
        "ON CONFLICT(email) DO UPDATE SET "
        "password_hash = excluded.password_hash, disabled_at = NULL;"
    )
    op_env = dict(deploy_env)
    op_env["CI"] = "1"
    op_proc = subprocess.run(
        [
            "npx",
            "--yes",
            "wrangler@4.147.0",
            "d1",
            "execute",
            STAGING_CONTROL_DB,
            "--remote",
            "-c",
            "apps/web/wrangler.staging.jsonc",
            "--command",
            sql,
        ],
        cwd=deploy_wt,
        env=op_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if op_proc.returncode != 0:
        detail = (op_proc.stderr or op_proc.stdout or "").strip()[-600:]
        raise RuntimeError(f"Staging operator seed failed (exit {op_proc.returncode}): {detail}")

    _ACCOUNTS_SEEDED = True


def deploy_to_staging(
    deploy_wt: str,
    deploy_env: dict[str, str],
    timeout_sec: int,
    extra_args: Sequence[str] = (),
) -> tuple[bool, str]:
    """Run scripts/deploy_staging.sh --apply --force inside deploy_wt."""
    cmd = ["bash", "scripts/deploy_staging.sh", "--apply", "--force", *extra_args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=deploy_wt,
            env=deploy_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired as exc:
        out = f"deploy_staging.sh timed out after {timeout_sec}s\n{exc.stdout or ''}\n{exc.stderr or ''}"
        return False, out


def check_staging_health(base_url: str, deploy_env: dict[str, str], retries: int = 5) -> bool:
    """Verify staging /health returns HTTP 200 through Cloudflare Access."""
    url = base_url.rstrip("/") + "/health"
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if deploy_env.get("CF_ACCESS_CLIENT_ID"):
        headers["CF-Access-Client-Id"] = deploy_env["CF_ACCESS_CLIENT_ID"]
    if deploy_env.get("CF_ACCESS_CLIENT_SECRET"):
        headers["CF-Access-Client-Secret"] = deploy_env["CF_ACCESS_CLIENT_SECRET"]

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError):
            pass
        if attempt < retries:
            time.sleep(3)
    return False


def rollback_staging_workers(
    deploy_wt: str,
    pre_web_ver: str,
    pre_ingest_ver: str,
    deploy_env: dict[str, str],
    base_url: str,
    job_id: str,
) -> tuple[bool, str]:
    """Roll back staging Workers to pre-deploy version IDs and verify /health."""
    logs: list[str] = []
    rollback_failed = False

    for worker_name, target_ver in (
        (STAGING_WEB_WORKER, pre_web_ver),
        (STAGING_INGEST_WORKER, pre_ingest_ver),
    ):
        if not target_ver:
            continue
        cur_ver = get_worker_active_version(deploy_wt, worker_name, deploy_env)
        if cur_ver and cur_ver == target_ver:
            logs.append(f"{worker_name} already at {target_ver}")
            continue
        proc = subprocess.run(
            [
                "npx",
                "--yes",
                "wrangler@4.147.0",
                "rollback",
                target_ver,
                "--name",
                worker_name,
                "--message",
                f"Factory QA rollback for {job_id}",
                "--yes",
            ],
            cwd=deploy_wt,
            env=deploy_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            rollback_failed = True
            logs.append(f"{worker_name} wrangler rollback failed: {out[-300:]}")
        else:
            logs.append(f"{worker_name} rolled back to {target_ver}")

    if rollback_failed:
        redeploy = subprocess.run(
            ["bash", "scripts/deploy_staging.sh", "--apply", "--force", "--skip-migrations"],
            cwd=deploy_wt,
            env=deploy_env,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if redeploy.returncode != 0:
            logs.append("Fallback redeploy of reverted main also failed.")
            return False, "\n".join(logs)
        logs.append("Fallback redeploy of reverted main succeeded.")

    healthy = check_staging_health(base_url, deploy_env)
    logs.append(f"Post-rollback /health check: {'OK' if healthy else 'FAIL'}")
    return healthy, "\n".join(logs)
