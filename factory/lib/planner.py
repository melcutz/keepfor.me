"""Queue-level dependency DAG planner for batch and parallel factory runs."""
from __future__ import annotations

import graphlib
import re
from collections.abc import Callable

_DEP_CLAUSE_RE = re.compile(
    r"\b(?:depends\s+on|blocked\s+by|after)\s+((?:#\d+(?:\s*(?:,|and)\s*)?)+)",
    re.IGNORECASE,
)
_ISSUE_REF_RE = re.compile(r"#(\d+)")


def parse_explicit_dependencies(issues: list[dict]) -> dict[int, set[int]]:
    """Extract explicit `#N` dependencies from issue bodies/titles within the batch."""
    batch_nums = {int(item["number"]) for item in issues}
    deps: dict[int, set[int]] = {num: set() for num in batch_nums}
    for item in issues:
        num = int(item["number"])
        text = f"{item.get('title') or ''}\n{item.get('body') or ''}"
        for match in _DEP_CLAUSE_RE.finditer(text):
            for ref in _ISSUE_REF_RE.findall(match.group(1)):
                dep_num = int(ref)
                if dep_num in batch_nums and dep_num != num:
                    deps[num].add(dep_num)
    return deps


def _has_path(deps: dict[int, set[int]], start: int, target: int) -> bool:
    """Return True if `start` transitively depends on `target`."""
    seen: set[int] = set()
    stack = list(deps.get(start, ()))
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur not in seen:
            seen.add(cur)
            stack.extend(deps.get(cur, ()))
    return False


_NON_CONFLICT_TOP_DIRS = {"tests", "test", "docs", "doc"}
_EXTENSIONLESS_FILES = {"Makefile", "Dockerfile", "Procfile", "Justfile"}


def _normalize_conflict_surfaces(paths: list[str]) -> list[str]:
    """Normalize predicted surface paths to high-signal conflict targets only.

    Broad directories (such as `tests/`, `templates/`, `src/pkg/`, or `static/`)
    and test/doc/hashed-bundle paths are ignored so independent issues are not
    chained sequentially. Ordered migration directories (`migrations/`,
    `control_migrations/`) and specific source/template files are retained.
    """
    normalized: list[str] = []
    for raw in paths:
        if not raw or not str(raw).strip():
            continue
        stripped = str(raw).strip()
        while stripped.startswith("./"):
            stripped = stripped[2:]
        stripped = stripped.lstrip("/")
        explicit_dir = stripped.endswith("/")
        clean = stripped.rstrip("/")
        if not clean:
            continue
        parts = [p for p in clean.split("/") if p]
        if not parts:
            continue
        if parts[0] in _NON_CONFLICT_TOP_DIRS:
            continue

        mig_idx = next(
            (
                idx
                for idx, seg in enumerate(parts)
                if seg in {"migrations", "control_migrations"} or seg.endswith("_migrations")
            ),
            None,
        )
        if mig_idx is not None:
            normalized.append("/".join(parts[: mig_idx + 1]))
            continue

        basename = parts[-1]
        if "static" in parts and (explicit_dir or "*" in basename or basename.startswith("tailwind")):
            continue
        if explicit_dir or "*" in basename:
            continue
        if "." not in basename and basename not in _EXTENSIONLESS_FILES and "bin" not in parts[:-1]:
            continue
        normalized.append("/".join(parts))
    return normalized


def _surfaces_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    """Return True if two predicted surface lists touch the same conflict target."""
    norm_a = _normalize_conflict_surfaces(paths_a)
    norm_b = _normalize_conflict_surfaces(paths_b)
    for pa in norm_a:
        for pb in norm_b:
            if pa == pb or pa.startswith(f"{pb}/") or pb.startswith(f"{pa}/"):
                return True
    return False


def _ensure_acyclic(deps: dict[int, set[int]]) -> dict[int, set[int]]:
    """Break any remaining cycles deterministically so TopologicalSorter succeeds."""
    clean = {k: set(v) for k, v in deps.items()}
    while True:
        try:
            graphlib.TopologicalSorter(clean).prepare()
            return clean
        except graphlib.CycleError as exc:
            cycle = exc.args[1] if len(exc.args) > 1 else []
            removed = False
            for idx in range(len(cycle) - 1):
                u, v = cycle[idx], cycle[idx + 1]
                # graphlib reports [node, dep, ..., node] or [dep, node, ...] depending on direction;
                # check both orientations and remove the first matching edge.
                if v in clean.get(u, ()):
                    clean[u].remove(v)
                    removed = True
                    break
                if u in clean.get(v, ()):
                    clean[v].remove(u)
                    removed = True
                    break
            if not removed:
                # Defensive fallback: clear one edge from the first node in the cycle.
                for node in clean:
                    if clean[node]:
                        clean[node].pop()
                        break
                else:
                    return clean


def plan_dependency_dag(
    issues: list[dict],
    *,
    planner_fn: Callable[[list[dict]], dict] | None = None,
) -> tuple[dict[int, set[int]], dict[int, set[int]], dict[int, list[str]]]:
    """Return `(order_deps, hard_deps, surfaces)` for a batch of issues.

    - `hard_deps`: true prerequisites (`Depends on #N` + LLM logical `edges`).
      Downstream issues should be skipped if a hard prerequisite fails to merge.
    - `order_deps`: `hard_deps` plus soft conflict-avoidance ordering edges from
      overlapping `predicted_surfaces`. Soft overlaps serialize execution to avoid
      concurrent edits on the same file, without skipping downstream jobs if an
      earlier job halts unmerged.
    """
    explicit = parse_explicit_dependencies(issues)
    hard_deps: dict[int, set[int]] = {k: set(v) for k, v in explicit.items()}
    surfaces: dict[int, list[str]] = {k: [] for k in hard_deps}
    batch_nums = set(hard_deps.keys())

    if len(issues) > 1 and planner_fn is not None:
        try:
            plan = planner_fn(issues) or {}
            candidate_hard = {k: set(v) for k, v in explicit.items()}
            for edge in plan.get("edges") or []:
                num = int(edge["issue"])
                if num not in batch_nums:
                    continue
                for dep in edge.get("depends_on") or []:
                    dep_num = int(dep)
                    if dep_num in batch_nums and dep_num != num:
                        candidate_hard[num].add(dep_num)

            graphlib.TopologicalSorter(candidate_hard).prepare()
            hard_deps = candidate_hard

            for surf in plan.get("predicted_surfaces") or []:
                num = int(surf["issue"])
                if num in batch_nums:
                    surfaces[num] = [str(p) for p in (surf.get("paths") or []) if str(p).strip()]

            order_deps = {k: set(v) for k, v in hard_deps.items()}
            ordered_nums = sorted(batch_nums)
            for idx, a in enumerate(ordered_nums):
                if not surfaces.get(a):
                    continue
                for b in ordered_nums[idx + 1 :]:
                    if not surfaces.get(b):
                        continue
                    if _surfaces_overlap(surfaces[a], surfaces[b]):
                        if not _has_path(order_deps, a, b) and not _has_path(order_deps, b, a):
                            order_deps[b].add(a)

            graphlib.TopologicalSorter(order_deps).prepare()
            return order_deps, hard_deps, surfaces
        except graphlib.CycleError:
            # Fall back to explicit dependencies if LLM/surface edges introduced a cycle.
            hard_deps = _ensure_acyclic({k: set(v) for k, v in explicit.items()})
            return {k: set(v) for k, v in hard_deps.items()}, hard_deps, surfaces
        except Exception:
            hard_deps = _ensure_acyclic({k: set(v) for k, v in explicit.items()})
            return {k: set(v) for k, v in hard_deps.items()}, hard_deps, surfaces

    clean_hard = _ensure_acyclic(hard_deps)
    return {k: set(v) for k, v in clean_hard.items()}, clean_hard, surfaces


def build_dependency_dag(
    issues: list[dict],
    *,
    planner_fn: Callable[[list[dict]], dict] | None = None,
) -> tuple[dict[int, set[int]], dict[int, list[str]]]:
    """Build an acyclic `{issue: {depends_on...}}` DAG and `{issue: [paths]}` map."""
    order_deps, _, surfaces = plan_dependency_dag(issues, planner_fn=planner_fn)
    return order_deps, surfaces
