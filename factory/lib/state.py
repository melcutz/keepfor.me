"""Atomic, thread-safe job.json state machine and resume semantics."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone

TRANSITIONS: dict[str, list[str]] = {
    "queued": ["intake", "rejected"],
    "intake": ["spec", "needs-info", "rejected", "needs-human"],
    "needs-info": ["intake", "rejected"],
    "spec": ["build", "needs-human"],
    "build": ["gate", "needs-human"],
    "gate": ["review", "build", "needs-human"],
    "review": ["approve", "build", "needs-human"],
    "approve": ["merging", "ready", "needs-human"],
    "merging": ["staging_qa", "merged", "needs-human"],
    "staging_qa": ["merged", "merging", "build", "needs-human"],
    "needs-human": ["intake", "spec", "build", "merging", "staging_qa", "rejected"],
    "ready": ["merged"],
    "merged": [],
    "rejected": [],
}

TERMINAL_OR_READY_STAGES: set[str] = {"merged", "ready", "rejected"}

_GLOBAL_LOCK = threading.Lock()
_JOB_LOCKS: dict[str, threading.RLock] = {}


class IllegalTransitionError(ValueError):
    """Raised when a job stage transition violates TRANSITIONS or round_limit."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(jobs_dir: str, job_id: str) -> str:
    return os.path.join(jobs_dir, job_id)


def _job_lock(jobs_dir: str, job_id: str) -> threading.RLock:
    key = os.path.abspath(_job_dir(jobs_dir, job_id))
    with _GLOBAL_LOCK:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _JOB_LOCKS[key] = lock
        return lock


def _atomic_write_job(jobs_dir: str, job_id: str, data: dict) -> dict:
    d = _job_dir(jobs_dir, job_id)
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, "job.json")
    fd, tmp_path = tempfile.mkstemp(prefix=".job.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return data


def load_job(jobs_dir: str, job_id: str) -> dict:
    with _job_lock(jobs_dir, job_id):
        path = os.path.join(_job_dir(jobs_dir, job_id), "job.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Job {job_id} not found at {path}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)


def update_job(jobs_dir: str, job_id: str, **fields) -> dict:
    with _job_lock(jobs_dir, job_id):
        job = load_job(jobs_dir, job_id)
        job.update(fields)
        job["updated"] = _now_iso()
        return _atomic_write_job(jobs_dir, job_id, job)


def advance_stage(
    jobs_dir: str,
    job_id: str,
    next_stage: str,
    last_error: str | None = None,
    preserve_last_error: bool = False,
    expand_round_by: int = 0,
) -> dict:
    with _job_lock(jobs_dir, job_id):
        job = load_job(jobs_dir, job_id)
        current = job["stage"]
        allowed = TRANSITIONS.get(current, [])
        if next_stage not in allowed:
            raise IllegalTransitionError(f"Illegal transition for {job_id}: {current} -> {next_stage}")

        next_round = job.get("round", 0)
        if current != "build" and next_stage == "build":
            if expand_round_by > 0:
                job["round_limit"] = max(job.get("round_limit", 3), next_round + int(expand_round_by))
            next_round += 1
            round_limit = job.get("round_limit", 3)
            if next_round > round_limit:
                raise IllegalTransitionError(
                    f"{job_id} exceeded round_limit ({next_round} > {round_limit}) on {current} -> build"
                )
            job["round"] = next_round

        if next_stage == "needs-human":
            job["escalated_from"] = current
        elif current == "needs-human":
            job["escalated_from"] = None
        job["stage"] = next_stage
        if next_stage in TERMINAL_OR_READY_STAGES or next_stage in {"needs-info", "needs-human"}:
            job["activity"] = None
            job["pid"] = None
        if last_error is not None:
            job["last_error"] = last_error
        elif not preserve_last_error and next_stage not in {"needs-human", "needs-info"}:
            job["last_error"] = None
        job["updated"] = _now_iso()
        return _atomic_write_job(jobs_dir, job_id, job)


def _has_accepted_intake(jdir: str) -> bool:
    path = os.path.join(jdir, "intake.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("decision") == "ACCEPT"
    except Exception:
        return False


def _has_valid_plan(jdir: str) -> bool:
    path = os.path.join(jdir, "plan.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            slices = json.load(f).get("slices")
            return isinstance(slices, list) and len(slices) >= 1
    except Exception:
        return False


def enqueue_job(
    jobs_dir: str,
    issue: dict,
    max_rounds: int,
    max_qa_rounds: int = 3,
    *,
    waiting_on: set[int] | list[int] | None = None,
    pid: int | None = None,
) -> dict:
    """Register or refresh a batch-queued issue in `job.json` before a worker starts it."""
    issue_num = int(issue["number"])
    job_id = f"job-{issue_num}"
    deps_sorted = sorted(int(x) for x in (waiting_on or ()) if int(x) != issue_num)
    activity_str = (
        f"queued (after #{',#'.join(str(x) for x in deps_sorted)})"
        if deps_sorted
        else "queued"
    )
    with _job_lock(jobs_dir, job_id):
        jdir = _job_dir(jobs_dir, job_id)
        path = os.path.join(jdir, "job.json")
        now = _now_iso()
        if not os.path.exists(path):
            job = {
                "id": job_id,
                "issue": issue_num,
                "issue_url": issue.get("url", ""),
                "issue_title": issue.get("title", ""),
                "branch": f"factory/{job_id}",
                "stage": "queued",
                "round": 0,
                "round_limit": max_rounds,
                "qa_round": 0,
                "qa_round_limit": max_qa_rounds,
                "risk": "low",
                "surfaces": [],
                "total_slices": 0,
                "completed_slices": 0,
                "pr_number": None,
                "pr_url": None,
                "prior_prs": [],
                "usage": {
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "total_tokens": 0,
                    "duration_seconds": 0.0,
                    "cost_usd": 0.0,
                },
                "last_error": None,
                "activity": activity_str,
                "pid": pid,
                "created": now,
                "updated": now,
            }
            return _atomic_write_job(jobs_dir, job_id, job)

        job = load_job(jobs_dir, job_id)
        if job.get("stage") in TERMINAL_OR_READY_STAGES:
            return job
        if issue.get("title"):
            job["issue_title"] = issue["title"]
        if issue.get("url"):
            job["issue_url"] = issue["url"]
        job["activity"] = activity_str
        job["pid"] = pid
        job["updated"] = now
        return _atomic_write_job(jobs_dir, job_id, job)


def create_or_resume_job(
    jobs_dir: str,
    issue: dict,
    max_rounds: int,
    max_qa_rounds: int = 3,
) -> tuple[dict, str]:
    issue_num = int(issue["number"])
    job_id = f"job-{issue_num}"
    with _job_lock(jobs_dir, job_id):
        jdir = _job_dir(jobs_dir, job_id)
        path = os.path.join(jdir, "job.json")
        if not os.path.exists(path):
            now = _now_iso()
            job = {
                "id": job_id,
                "issue": issue_num,
                "issue_url": issue.get("url", ""),
                "issue_title": issue.get("title", ""),
                "branch": f"factory/{job_id}",
                "stage": "intake",
                "round": 0,
                "round_limit": max_rounds,
                "qa_round": 0,
                "qa_round_limit": max_qa_rounds,
                "risk": "low",
                "surfaces": [],
                "total_slices": 0,
                "completed_slices": 0,
                "pr_number": None,
                "pr_url": None,
                "prior_prs": [],
                "usage": {
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "thinking_tokens": 0,
                    "total_tokens": 0,
                    "duration_seconds": 0.0,
                    "cost_usd": 0.0,
                },
                "last_error": None,
                "activity": None,
                "pid": None,
                "created": now,
                "updated": now,
            }
            return _atomic_write_job(jobs_dir, job_id, job), "new"

        job = load_job(jobs_dir, job_id)
        job.setdefault("qa_round", 0)
        job.setdefault("qa_round_limit", max_qa_rounds)
        job.setdefault("prior_prs", [])
        job.setdefault("activity", None)
        job.setdefault("pid", None)
        if issue.get("title"):
            job["issue_title"] = issue["title"]
        if issue.get("url"):
            job["issue_url"] = issue["url"]

        stage = job["stage"]
        if stage in TERMINAL_OR_READY_STAGES:
            return job, "skip"
        if stage == "queued":
            _atomic_write_job(jobs_dir, job_id, job)
            return advance_stage(jobs_dir, job_id, "intake"), "new"
        if stage == "needs-info":
            _atomic_write_job(jobs_dir, job_id, job)
            return advance_stage(jobs_dir, job_id, "intake"), "resume"
        if stage == "needs-human":
            escalated_from = job.get("escalated_from")
            has_pipeline_artifacts = any(
                os.path.exists(os.path.join(jdir, name))
                for name in ("issue.md", "intake.json", "plan.json", "spec.json")
            )
            if escalated_from == "intake" or (has_pipeline_artifacts and not _has_accepted_intake(jdir)):
                _atomic_write_job(jobs_dir, job_id, job)
                return advance_stage(jobs_dir, job_id, "intake", preserve_last_error=True), "resume"
            if escalated_from == "spec" or (has_pipeline_artifacts and not _has_valid_plan(jdir)):
                _atomic_write_job(jobs_dir, job_id, job)
                return advance_stage(jobs_dir, job_id, "spec", preserve_last_error=True), "resume"
            if escalated_from == "merging" and job.get("pr_number"):
                _atomic_write_job(jobs_dir, job_id, job)
                return advance_stage(jobs_dir, job_id, "merging", preserve_last_error=True), "resume"
            if escalated_from == "staging_qa":
                job["qa_round_limit"] = max(job.get("qa_round_limit", max_qa_rounds), job.get("qa_round", 0) + 1)
            job["round_limit"] = max(job.get("round_limit", max_rounds), job.get("round", 0) + 1)
            _atomic_write_job(jobs_dir, job_id, job)
            return advance_stage(jobs_dir, job_id, "build", preserve_last_error=True), "resume"

        job["updated"] = _now_iso()
        return _atomic_write_job(jobs_dir, job_id, job), "resume"


def record_usage(
    jobs_dir: str,
    job_id: str,
    usage: dict,
    duration_seconds: float,
    cost_usd: float | None = None,
) -> dict:
    with _job_lock(jobs_dir, job_id):
        job = load_job(jobs_dir, job_id)
        cur = job.setdefault(
            "usage",
            {
                "input_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "total_tokens": 0,
                "duration_seconds": 0.0,
                "cost_usd": 0.0,
            },
        )
        for key in ("input_tokens", "cache_read_tokens", "output_tokens", "thinking_tokens", "total_tokens"):
            cur[key] = int(cur.get(key, 0)) + int((usage or {}).get(key, 0) or 0)
        cur["duration_seconds"] = round(float(cur.get("duration_seconds", 0.0)) + float(duration_seconds or 0.0), 3)
        if cost_usd is not None:
            cur["cost_usd"] = round(float(cur.get("cost_usd", 0.0) or 0.0) + float(cost_usd), 6)
        job["updated"] = _now_iso()
        return _atomic_write_job(jobs_dir, job_id, job)


def list_jobs(jobs_dir: str) -> list[dict]:
    if not os.path.isdir(jobs_dir):
        return []
    jobs = []
    for entry in sorted(os.listdir(jobs_dir)):
        path = os.path.join(jobs_dir, entry, "job.json")
        if os.path.isfile(path):
            jobs.append(load_job(jobs_dir, entry))
    jobs.sort(key=lambda j: j.get("issue", 0))
    return jobs
