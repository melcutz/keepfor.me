"""Configuration loading, validation, and reviewer model routing."""
from __future__ import annotations

import copy
import json
import os
from urllib.parse import urlparse

VALID_MERGE_METHODS = {"merge", "squash", "rebase"}
VALID_AUTO_MERGE = {"low", "all", "none"}
VALID_RISKS = {"low", "medium", "high"}
VALID_SURFACES = {"ui", "security"}
FORBIDDEN_PROD_HOSTS = {"app.keepfor.me", "ops.keepfor.me", "keepfor.me"}
EFFORT_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "max", "none"}

DEFAULT_MODEL_PRICING: dict[str, dict[str, float]] = {
    "gemini-3.6-flash": {"input_per_m": 0.15, "cached_per_m": 0.0375, "output_per_m": 0.60},
    "gemini-3.8-flash": {"input_per_m": 0.30, "cached_per_m": 0.075, "output_per_m": 2.50},
    "gemini-4-hydrogen": {"input_per_m": 0.30, "cached_per_m": 0.075, "output_per_m": 2.50},
    "gemini-4-argon": {"input_per_m": 1.25, "cached_per_m": 0.3125, "output_per_m": 10.00},
}

DEFAULT_CONFIG: dict = {
    "concurrency": 4,
    "max_rounds": 3,
    "max_slice_retries": 2,
    "agent_timeout_sec": 900,
    "gate_timeout_sec": 600,
    "base_branch": "main",
    "merge_method": "merge",
    "auto_merge": "low",
    "auto_polish": True,
    "auto_approve_clean": True,
    "idle_bg_grace_sec": 45,
    "labels": {
        "queue": "factory",
        "needs_info": "factory:needs-info",
        "needs_human": "factory:needs-human",
    },
    "protected_paths": [
        "factory/",
        ".agents/",
        ".github/",
    ],
    "models": {
        "planner": "gemini-3.8-flash-medium",
        "intake": "gemini-3.8-flash-medium",
        "spec_writer": "gemini-4-argon-high",
        "builder": "gemini-4-argon-high",
        "approver": "gemini-4-argon-high",
        "qa": "gemini-4-argon-high",
        "code_low": "gemini-3.6-flash-low",
        "code_medium": "gemini-3.8-flash-low",
        "code_high": "gemini-3.8-flash-medium",
        "security": "gemini-4-argon-high",
        "ux": "gemini-3.8-flash-medium",
        "ui": "gemini-3.6-flash-low",
    },
    "model_pricing": copy.deepcopy(DEFAULT_MODEL_PRICING),
    "staging_qa": {
        "enabled": False,
        "max_batch_size": 4,
        "coalesce_window_sec": 15,
        "max_qa_rounds": 3,
        "deploy_timeout_sec": 900,
        "qa_timeout_sec": 1200,
        "base_url": "https://staging.keepfor.me",
        "ops_base_url": "https://ops-staging.keepfor.me",
        "env_files": [],
    },
}


def split_model_and_effort(model_spec: str) -> tuple[str, str]:
    """Split a model identifier like 'gemini-4-argon-high' into ('gemini-4-argon', 'high')."""
    spec = (model_spec or "").strip()
    if not spec:
        return "unknown", "default"
    if "-" in spec:
        base, suffix = spec.rsplit("-", 1)
        if base and suffix.lower() in EFFORT_LEVELS:
            return base, suffix.lower()
    return spec, "default"


def estimate_usage_cost_usd(
    model_spec: str,
    usage: dict | None,
    config: dict | None = None,
) -> float | None:
    """Return direct or estimated USD cost for a single agent step's token usage, or None if unknown."""
    if not isinstance(usage, dict):
        return None
    for direct_key in ("cost_usd", "cost"):
        val = usage.get(direct_key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return round(float(val), 6)

    pricing_map = ((config or {}).get("model_pricing") if isinstance(config, dict) else None) or DEFAULT_MODEL_PRICING
    base_model, _ = split_model_and_effort(model_spec)
    rates = pricing_map.get(model_spec) or pricing_map.get(base_model)
    if not isinstance(rates, dict):
        return None

    in_tok = int(usage.get("input_tokens", 0) or 0)
    cache_tok = int(usage.get("cache_read_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    if in_tok == 0 and cache_tok == 0 and out_tok == 0:
        tot = int(usage.get("total_tokens", 0) or 0)
        if tot == 0:
            return 0.0
        in_tok = tot

    in_rate = float(rates.get("input_per_m", 0.0))
    cache_rate = float(rates.get("cached_per_m", in_rate * 0.25))
    out_rate = float(rates.get("output_per_m", 0.0))
    cost = (in_tok * in_rate + cache_tok * cache_rate + out_tok * out_rate) / 1_000_000.0
    return round(cost, 6)


def _validate_non_prod_url(url: str, field_name: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"{field_name} must be a non-empty URL string")
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError(f"{field_name} must be an https URL, got {url!r}")
    if host in FORBIDDEN_PROD_HOSTS:
        raise ValueError(f"Refusing production URL in {field_name}: {url!r}")


def load_config(factory_dir: str) -> dict:
    """Load and validate factory/config.json merged with DEFAULT_CONFIG."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = os.path.join(factory_dir, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config.json root must be a JSON object")
        for key, value in raw.items():
            if key in {"labels", "models", "model_pricing", "staging_qa"} and isinstance(value, dict):
                cfg[key].update(value)
            else:
                cfg[key] = value

    if cfg["merge_method"] not in VALID_MERGE_METHODS:
        raise ValueError(f"Invalid merge_method: {cfg['merge_method']!r}")
    if cfg["auto_merge"] not in VALID_AUTO_MERGE:
        raise ValueError(f"Invalid auto_merge: {cfg['auto_merge']!r}")

    for int_key in ("concurrency", "max_rounds", "max_slice_retries", "agent_timeout_sec", "gate_timeout_sec"):
        val = cfg.get(int_key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"{int_key} must be a positive integer, got {val!r}")

    for bool_key in ("auto_polish", "auto_approve_clean"):
        val = cfg.get(bool_key)
        if not isinstance(val, bool):
            raise ValueError(f"{bool_key} must be a boolean, got {val!r}")

    idle_grace = cfg.get("idle_bg_grace_sec")
    if not isinstance(idle_grace, int) or isinstance(idle_grace, bool) or idle_grace < 5:
        raise ValueError(f"idle_bg_grace_sec must be an integer >= 5, got {idle_grace!r}")

    if not isinstance(cfg.get("base_branch"), str) or not cfg["base_branch"].strip():
        raise ValueError("base_branch must be a non-empty string")
    if not isinstance(cfg.get("protected_paths"), list):
        raise ValueError("protected_paths must be a list of path patterns")

    sqa = cfg.get("staging_qa")
    if not isinstance(sqa, dict):
        raise ValueError("staging_qa must be a JSON object")
    if not isinstance(sqa.get("enabled"), bool):
        raise ValueError("staging_qa.enabled must be a boolean")
    for int_key in ("max_batch_size", "max_qa_rounds", "deploy_timeout_sec", "qa_timeout_sec"):
        val = sqa.get(int_key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 1:
            raise ValueError(f"staging_qa.{int_key} must be a positive integer, got {val!r}")
    coalesce_sec = sqa.get("coalesce_window_sec")
    if not isinstance(coalesce_sec, int) or isinstance(coalesce_sec, bool) or coalesce_sec < 0:
        raise ValueError(f"staging_qa.coalesce_window_sec must be an integer >= 0, got {coalesce_sec!r}")
    _validate_non_prod_url(sqa.get("base_url", ""), "staging_qa.base_url")
    _validate_non_prod_url(sqa.get("ops_base_url", ""), "staging_qa.ops_base_url")
    if not isinstance(sqa.get("env_files"), list) or any(not isinstance(p, str) for p in sqa["env_files"]):
        raise ValueError("staging_qa.env_files must be a list of file paths")

    return cfg


def resolve_reviewers(config: dict, risk: str, surfaces: list[str]) -> dict[str, str]:
    """Deterministically resolve reviewer roles to model IDs for a given risk and surface set."""
    norm_risk = (risk or "low").strip().lower()
    if norm_risk not in VALID_RISKS:
        norm_risk = "low"

    models = config["models"]
    reviewers: dict[str, str] = {
        "code": models[f"code_{norm_risk}"],
    }

    norm_surfaces = {s.strip().lower() for s in (surfaces or []) if isinstance(s, str)}
    if "both" in norm_surfaces:
        norm_surfaces = {"ui", "security"}

    if "security" in norm_surfaces:
        reviewers["security"] = models["security"]
    if "ui" in norm_surfaces:
        reviewers["ux"] = models["ux"]
        reviewers["ui"] = models["ui"]

    return reviewers
