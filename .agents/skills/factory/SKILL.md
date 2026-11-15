---
name: factory
description: Run the autonomous software factory on GitHub issues. Use when the user types /factory run, /factory run <n>, /factory status, /factory doctor, /factory research <topic>, or /factory improve.
---

# Autonomous Software Factory (`agy`)

You operate the repository's autonomous software factory (`factory/bin/factory`). The deterministic Python runner manages git worktrees, `agy -p --output-format json --json-schema` worker invocations, vertical TDD slice execution, parallel reviews, GitHub Pull Requests, and post-merge Staging Deploy + Playwright Browser QA (`staging_qa`) against `https://staging.keepfor.me` and `https://ops-staging.keepfor.me` (with automatic `origin/main` revert, Cloudflare staging Worker rollback, and rebuild retry up to `max_qa_rounds` on any QA failure).

## Commands

### `/factory run` or `/factory run <issue-number>`
Drain all eligible open `factory`-labeled GitHub issues (up to `concurrency` in `factory/config.json`), or process a single issue number:

```bash
./factory/bin/factory run          # drain all eligible queued issues
./factory/bin/factory run 42       # run or resume only issue #42
```

- Long multi-slice runs can take several minutes; run with an appropriate command wait or monitor progress via `./factory/bin/factory status`.
- When finished, summarize each processed job's final stage (`merged`, `ready`, `needs-info`, `needs-human`, `rejected`), PR link, slice count, and any question needing human input.

### `/factory status [job-id]`
Show the summary table of all jobs (stage, round, completed slices, risk, tokens, PR URL) or inspect one job's `job.json`:

```bash
./factory/bin/factory status
./factory/bin/factory status job-42
```

### `/factory queue`
List open `factory`-labeled issues eligible for execution (automatically excludes `factory:needs-info` and `factory:needs-human`):

```bash
./factory/bin/factory queue
```

### `/factory doctor`
Verify all prerequisites (`python3 >= 3.10`, `git` + `origin`, `gh auth status`, `agy`, `factory/config.json`, executable `factory/checks.sh`, and `.gitignore`):

```bash
./factory/bin/factory doctor
```

### `/factory research <topic>`
Read and follow `factory/agents/researcher.md` directly in this `agy` session: investigate primary sources (using `agy`'s read-only `research` subagent for broad exploration if helpful) and save a cited Markdown report under `docs/research/`.

### `/factory improve [area]`
Read and follow `factory/agents/architect.md` interactively in this `agy` session: survey high-churn modules for deepening opportunities, generate the visual HTML report in `${TMPDIR:-/tmp}/`, and walk through candidate trade-offs live with the user.
