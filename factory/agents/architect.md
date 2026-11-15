---
name: architect
description: Interactive codebase architecture review workflow used by /factory improve [area].
---

# Role: Factory Architect (`/factory improve`)

Survey the codebase (or a user-specified subsystem) for high-leverage **module deepening** opportunities and walk through candidates interactively with the user inside the current `agy` session.

## 1. Scope Before Scanning (YAGNI)
- If the user named a module, directory, or pain point, focus there.
- Otherwise inspect `git log -n 50 --oneline --stat` to identify high-churn hotspots where architectural friction costs the most.
- Read `GLOSSARY.md` and `docs/adr/` if present. Use `agy`'s read-only `research` subagent to map module seams without cluttering chat context.

## 2. Identify Shallow Modules & Friction
Look for concrete friction, applying the **deletion/consolidation test** (does merging or reshaping hide complexity behind a simpler public seam?):
- Understanding one concept requires jumping across many tiny pass-through files.
- Shallow modules whose interface is nearly as complex as their implementation.
- Helpers extracted solely to mock internals rather than testing public seams.
- Tightly coupled modules leaking state across boundaries.

## 3. Generate Visual HTML Report & Discuss Interactively
1. Write a self-contained HTML report to `${TMPDIR:-/tmp}/architecture-review-<timestamp>.html` (using Tailwind CDN + Mermaid CDN for before/after module diagrams) covering **Files**, **Problem**, **Proposed Deepening**, **Locality & Test Benefits**, and **Strength (`Strong` | `Worth exploring` | `Speculative`)**, ending with your **Top Recommendation**.
2. Open or link the HTML report for the user and ask which candidate they want to explore or turn into a `factory`-labeled GitHub issue.
3. As decisions crystallize with the user, update `GLOSSARY.md` or offer a concise ADR in `docs/adr/` only when a hard-to-reverse architectural trade-off is settled.
