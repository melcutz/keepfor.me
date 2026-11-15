"""Structured output JSON Schemas, validation, and Markdown renderers."""
from __future__ import annotations

import copy

SCHEMAS: dict[str, dict] = {
    "plan": {
        "type": "object",
        "additionalProperties": False,
        "required": ["edges", "predicted_surfaces", "reasoning"],
        "properties": {
            "edges": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["issue", "depends_on"],
                    "properties": {
                        "issue": {"type": "integer"},
                        "depends_on": {"type": "array", "items": {"type": "integer"}},
                    },
                },
            },
            "predicted_surfaces": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["issue", "paths"],
                    "properties": {
                        "issue": {"type": "integer"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "reasoning": {"type": "string"},
        },
    },
    "intake": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "decision",
            "risk",
            "surfaces",
            "summary",
            "scope_in",
            "scope_out",
            "questions",
            "reasoning",
        ],
        "properties": {
            "decision": {"type": "string", "enum": ["ACCEPT", "NEEDS_INFO", "REJECT"]},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "surfaces": {
                "type": "array",
                "items": {"type": "string", "enum": ["ui", "security"]},
            },
            "summary": {"type": "array", "items": {"type": "string"}},
            "scope_in": {"type": "string"},
            "scope_out": {"type": "string"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string"},
        },
    },
    "spec": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "problem",
            "solution",
            "user_stories",
            "implementation_decisions",
            "testing_decisions",
            "glossary_updates",
            "out_of_scope",
            "slices",
        ],
        "properties": {
            "summary": {"type": "array", "items": {"type": "string"}},
            "problem": {"type": "string"},
            "solution": {"type": "string"},
            "user_stories": {"type": "array", "items": {"type": "string"}},
            "implementation_decisions": {"type": "array", "items": {"type": "string"}},
            "testing_decisions": {"type": "array", "items": {"type": "string"}},
            "glossary_updates": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
            "slices": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "title", "goal", "files", "test_seams", "acceptance_criteria"],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "goal": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "test_seams": {"type": "array", "items": {"type": "string"}},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    },
    "build": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "summary", "changes", "tests_added", "blocked_reason", "notes"],
        "properties": {
            "verdict": {"type": "string", "enum": ["DONE", "BLOCKED"]},
            "summary": {"type": "array", "items": {"type": "string"}},
            "changes": {"type": "array", "items": {"type": "string"}},
            "tests_added": {"type": "array", "items": {"type": "string"}},
            "blocked_reason": {"type": "string"},
            "notes": {"type": "string"},
        },
    },
    "review": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "summary", "findings", "checked"],
        "properties": {
            "verdict": {"type": "string", "enum": ["PASS", "CHANGES"]},
            "summary": {"type": "array", "items": {"type": "string"}},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["severity", "file", "line", "issue", "fix"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["must_fix", "should_fix", "nit"]},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "issue": {"type": "string"},
                        "fix": {"type": "string"},
                    },
                },
            },
            "checked": {"type": "array", "items": {"type": "string"}},
        },
    },
    "approve": {
        "type": "object",
        "additionalProperties": False,
        "required": ["decision", "summary", "reasoning", "merge_danger", "question"],
        "properties": {
            "decision": {"type": "string", "enum": ["APPROVE", "ESCALATE"]},
            "summary": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string"},
            "merge_danger": {"type": "string"},
            "question": {"type": "string"},
        },
    },
    "qa": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "verdict",
            "summary",
            "scenarios_tested",
            "personas_used",
            "failures",
            "reproduction_steps",
        ],
        "properties": {
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "summary": {"type": "array", "items": {"type": "string"}},
            "scenarios_tested": {"type": "array", "items": {"type": "string"}},
            "personas_used": {"type": "array", "items": {"type": "string"}},
            "failures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["scenario", "expected", "actual", "url", "evidence"],
                    "properties": {
                        "scenario": {"type": "string"},
                        "expected": {"type": "string"},
                        "actual": {"type": "string"},
                        "url": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "reproduction_steps": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def _require_keys(stage: str, payload: dict, required: list[str]) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{stage} output must be a JSON object")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"{stage} output missing required keys: {missing}")


def validate_stage_output(stage: str, payload: dict) -> dict:
    """Validate structured output for a stage and normalize anti-churn rules."""
    if stage not in SCHEMAS:
        raise ValueError(f"Unknown schema stage: {stage!r}")
    data = copy.deepcopy(payload)
    schema = SCHEMAS[stage]
    _require_keys(stage, data, schema["required"])

    if stage == "intake":
        if data["decision"] not in {"ACCEPT", "NEEDS_INFO", "REJECT"}:
            raise ValueError(f"Invalid intake decision: {data['decision']!r}")
        if data["risk"] not in {"low", "medium", "high"}:
            raise ValueError(f"Invalid intake risk: {data['risk']!r}")
        if not isinstance(data["surfaces"], list) or any(s not in {"ui", "security"} for s in data["surfaces"]):
            raise ValueError(f"Invalid intake surfaces: {data['surfaces']!r}")
        data["surfaces"] = sorted(set(data["surfaces"]))
        if data["decision"] == "NEEDS_INFO" and not [q for q in data["questions"] if str(q).strip()]:
            raise ValueError("NEEDS_INFO intake must provide at least one question")

    elif stage == "spec":
        slices = data.get("slices")
        if not isinstance(slices, list) or not (1 <= len(slices) <= 6):
            raise ValueError("spec slices must contain between 1 and 6 vertical slices")
        slice_req = schema["properties"]["slices"]["items"]["required"]
        for idx, sl in enumerate(slices):
            _require_keys(f"spec.slices[{idx}]", sl, slice_req)
            if not str(sl["title"]).strip() or not str(sl["goal"]).strip():
                raise ValueError(f"spec.slices[{idx}] must have non-empty title and goal")

    elif stage == "build":
        if data["verdict"] not in {"DONE", "BLOCKED"}:
            raise ValueError(f"Invalid build verdict: {data['verdict']!r}")
        if data["verdict"] == "BLOCKED" and not str(data["blocked_reason"]).strip():
            raise ValueError("BLOCKED build must specify blocked_reason")

    elif stage == "review":
        if data["verdict"] not in {"PASS", "CHANGES"}:
            raise ValueError(f"Invalid review verdict: {data['verdict']!r}")
        if not isinstance(data["findings"], list):
            raise ValueError("review findings must be a list")
        finding_req = schema["properties"]["findings"]["items"]["required"]
        actionable = 0
        for idx, finding in enumerate(data["findings"]):
            _require_keys(f"review.findings[{idx}]", finding, finding_req)
            sev = finding["severity"]
            if sev not in {"must_fix", "should_fix", "nit"}:
                raise ValueError(f"Invalid finding severity: {sev!r}")
            if sev in {"must_fix", "should_fix"}:
                actionable += 1
        # Anti-churn rule: nit-only reviews normalize to PASS; actionable findings force CHANGES
        if actionable == 0:
            data["verdict"] = "PASS"
        elif data["verdict"] == "PASS":
            data["verdict"] = "CHANGES"

    elif stage == "approve":
        if data["decision"] not in {"APPROVE", "ESCALATE"}:
            raise ValueError(f"Invalid approve decision: {data['decision']!r}")
        if data["decision"] == "ESCALATE" and not str(data["question"]).strip():
            raise ValueError("ESCALATE decision must include a non-empty question")

    elif stage == "qa":
        if data["verdict"] not in {"PASS", "FAIL"}:
            raise ValueError(f"Invalid qa verdict: {data['verdict']!r}")
        scenarios = [str(s).strip() for s in (data.get("scenarios_tested") or []) if str(s).strip()]
        if not scenarios:
            raise ValueError("qa scenarios_tested must include at least one executed scenario")
        data["scenarios_tested"] = scenarios
        if not isinstance(data.get("failures"), list):
            raise ValueError("qa failures must be a list")
        fail_req = schema["properties"]["failures"]["items"]["required"]
        for idx, fl in enumerate(data["failures"]):
            _require_keys(f"qa.failures[{idx}]", fl, fail_req)
        if data["failures"]:
            data["verdict"] = "FAIL"
        if data["verdict"] == "FAIL":
            if not data["failures"]:
                raise ValueError("FAIL qa verdict must provide at least one failure entry")
            repro = [str(r).strip() for r in (data.get("reproduction_steps") or []) if str(r).strip()]
            if not repro:
                raise ValueError("FAIL qa verdict must provide reproduction_steps")
            data["reproduction_steps"] = repro

    return data


def _bullets(items: list[str], fallback: str = "None") -> str:
    cleaned = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not cleaned:
        return f"- {fallback}"
    return "\n".join(f"- {item}" for item in cleaned)


def render_intake_md(data: dict) -> str:
    surfaces = ", ".join(data.get("surfaces") or []) or "none"
    parts = [
        f"DECISION: {data['decision']}",
        f"RISK: {data['risk']}",
        f"SURFACES: {surfaces}",
        "",
        "## Summary",
        _bullets(data.get("summary", [])),
        "",
        "## Scope In",
        data.get("scope_in", "").strip() or "N/A",
        "",
        "## Scope Out",
        data.get("scope_out", "").strip() or "N/A",
        "",
        "## Reasoning",
        data.get("reasoning", "").strip() or "N/A",
    ]
    if data.get("questions"):
        parts.extend(["", "## Questions", _bullets(data["questions"])])
    return "\n".join(parts) + "\n"


def render_spec_md(data: dict) -> str:
    slice_blocks = []
    for idx, sl in enumerate(data.get("slices", []), start=1):
        slice_blocks.append(
            "\n".join(
                [
                    f"### Slice {idx}: {sl['title']} (`{sl['id']}`)",
                    f"**Goal:** {sl['goal']}",
                    "**Files:**",
                    _bullets(sl.get("files", [])),
                    "**Test Seams:**",
                    _bullets(sl.get("test_seams", [])),
                    "**Acceptance Criteria:**",
                    _bullets(sl.get("acceptance_criteria", [])),
                ]
            )
        )
    parts = [
        "## Summary",
        _bullets(data.get("summary", [])),
        "",
        "## Problem Statement",
        data.get("problem", "").strip(),
        "",
        "## Solution",
        data.get("solution", "").strip(),
        "",
        "## User Stories",
        _bullets(data.get("user_stories", [])),
        "",
        "## Implementation Decisions",
        _bullets(data.get("implementation_decisions", [])),
        "",
        "## Testing Decisions",
        _bullets(data.get("testing_decisions", [])),
        "",
        "## Glossary & ADR Updates",
        _bullets(data.get("glossary_updates", []), fallback="No glossary or ADR updates required."),
        "",
        "## Out of Scope",
        _bullets(data.get("out_of_scope", [])),
        "",
        "## Vertical Slices",
        "\n\n".join(slice_blocks),
    ]
    return "\n".join(parts) + "\n"


def render_build_md(data: dict, heading: str = "Build Summary") -> str:
    parts = [
        f"VERDICT: {data['verdict']}",
        "",
        f"## {heading}",
        _bullets(data.get("summary", [])),
        "",
        "## Changes",
        _bullets(data.get("changes", [])),
        "",
        "## Tests Added",
        _bullets(data.get("tests_added", [])),
    ]
    if data.get("blocked_reason"):
        parts.extend(["", "## Blocked Reason", data["blocked_reason"].strip()])
    if data.get("notes"):
        parts.extend(["", "## Notes", data["notes"].strip()])
    return "\n".join(parts) + "\n"


def render_review_md(reviewer_name: str, data: dict) -> str:
    finding_lines = []
    for f in data.get("findings", []):
        finding_lines.append(
            f"- **[{f['severity'].upper()}]** `{f['file']}:{f['line']}` -- {f['issue']} *(Fix: {f['fix']})*"
        )
    findings_block = "\n".join(finding_lines) if finding_lines else "- No issues found."
    parts = [
        f"VERDICT: {data['verdict']}",
        f"REVIEWER: {reviewer_name}",
        "",
        "## Summary",
        _bullets(data.get("summary", [])),
        "",
        "## Findings",
        findings_block,
        "",
        "## Checked",
        _bullets(data.get("checked", [])),
    ]
    return "\n".join(parts) + "\n"


def render_approve_md(data: dict) -> str:
    parts = [
        f"DECISION: {data['decision']}",
        "",
        "## Summary",
        _bullets(data.get("summary", [])),
        "",
        "## Reasoning",
        data.get("reasoning", "").strip(),
        "",
        "## Merge Danger",
        data.get("merge_danger", "").strip() or "N/A",
    ]
    if data.get("question"):
        parts.extend(["", "## Question", data["question"].strip()])
    return "\n".join(parts) + "\n"


def render_pr_body(
    job: dict,
    intake: dict,
    spec: dict,
    reviews: dict[str, dict] | None = None,
    approve: dict | None = None,
) -> str:
    review_lines = []
    for name, rev in sorted((reviews or {}).items()):
        review_lines.append(f"- **{name}**: `{rev.get('verdict', 'PENDING')}`")
    review_section = "\n".join(review_lines) if review_lines else "- Pending review stage"
    merge_danger = (approve or {}).get("merge_danger") or f"Risk tier assessed at intake: `{job.get('risk', 'low')}`."

    parts = [
        f"Closes #{job['issue']}",
        "",
        "## Summary",
        _bullets(spec.get("summary") or intake.get("summary") or []),
        "",
        "## Verification Evidence",
        f"- Deterministic gate (`factory/checks.sh` + protected paths): **PASS** (round {job.get('round', 1)})",
        f"- Vertical TDD slices completed: `{job.get('completed_slices', 0)}/{job.get('total_slices', 0)}`",
        review_section,
        "",
        "## Merge-Danger Assessment",
        merge_danger,
    ]
    return "\n".join(parts) + "\n"


def render_qa_md(data: dict) -> str:
    personas = ", ".join(data.get("personas_used") or []) or "none"
    failure_lines = []
    for f in data.get("failures", []):
        failure_lines.append(
            f"- **{f['scenario']}** (`{f['url']}`): expected {f['expected']}; "
            f"actual {f['actual']} *(Evidence: `{f['evidence']}`)*"
        )
    failures_block = "\n".join(failure_lines) if failure_lines else "- No failures detected."
    parts = [
        f"VERDICT: {data['verdict']}",
        f"PERSONAS: {personas}",
        "",
        "## Summary",
        _bullets(data.get("summary", [])),
        "",
        "## Scenarios Tested",
        _bullets(data.get("scenarios_tested", [])),
        "",
        "## Failures",
        failures_block,
    ]
    if data.get("reproduction_steps"):
        parts.extend(["", "## Reproduction Steps", _bullets(data["reproduction_steps"])])
    return "\n".join(parts) + "\n"


def render_qa_failure_md(
    qa_round: int,
    max_qa_rounds: int,
    reason: str,
    qa_data: dict | None = None,
) -> str:
    parts = [
        f"Staging QA cycle {qa_round}/{max_qa_rounds} failed (`origin/main` and staging Workers rolled back).",
        "",
        "### Failure Summary",
        reason.strip(),
    ]
    if qa_data:
        if qa_data.get("failures"):
            parts.extend(["", "### Failed Scenarios"])
            for f in qa_data["failures"]:
                parts.append(
                    f"- **{f['scenario']}** (`{f['url']}`): expected `{f['expected']}`, "
                    f"got `{f['actual']}` (evidence: `{f['evidence']}`)"
                )
        if qa_data.get("reproduction_steps"):
            parts.extend(["", "### Reproduction Steps", _bullets(qa_data["reproduction_steps"])])
    return "\n".join(parts) + "\n"
