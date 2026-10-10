# Contributing to Keepfor.me

Thank you for your interest in contributing to Keepfor.me! We welcome contributions, bug reports, feature requests, and pull requests.

---

## Contributor License Agreement (CLA)

All contributors must sign our [Contributor License Agreement (CLA)](CLA.md) before their pull requests can be merged.

### Key Points of the CLA

* **You keep your copyright**: You retain full ownership and copyright of your code and contributions. You are not assigning your copyright to us.
* **Why we require a CLA**: Keepfor.me is open source under the [AGPL-3.0 license](LICENSE). To ensure the long-term sustainability of the project, the project maintainer (Claudiu Branzan) needs the legal flexibility to:
  1. Operate and maintain the official hosted cloud service ([keepfor.me](https://keepfor.me) / [app.keepfor.me](https://app.keepfor.me)); and
  2. Offer commercial and enterprise dual-licensing options to organizations that require proprietary deployment terms outside the network-copyleft obligations of the AGPL.
* **You only sign once**: Every contributor signs the CLA just once. Once signed, all your future contributions and pull requests are covered automatically.
* **Automated signing via GitHub**: When you submit your first pull request, our automated GitHub Action (`CLA Assistant`) will check whether you have signed. If you haven't yet, it will leave a comment on your pull request with a link to [CLA.md](CLA.md) and instructions to sign by simply posting a comment (e.g., `"I have read the CLA Document and I hereby sign the CLA"`).
* **Company & Employer Clause**: If your employer owns the intellectual property for code you write, please ensure you have authorization to contribute or that your employer has waived rights before signing.

The CLA check is a required status check on the `main` branch; pull requests cannot be merged until it passes. The CLA applies to all human contributions (automated dependency bots excepted). The CLA text may be updated periodically for future contributions; previously signed versions remain valid for the contributions made under them.

---

## Development Setup & Workflow

### 1. Prerequisites

* Python 3.11+
* [uv](https://github.com/astral-sh/uv) or `pip`
* Node.js 22+ (for Wrangler CLI)

### 2. Running Tests

Run the test suite from the repository root:

```bash
python3 -m pytest tests/ -q
```

> **Note**: Always run `pytest` from the repo root. Subdirectories will fail to resolve `src` imports as `src` is a namespace package.

### 3. Code Style & Formatting

CI enforces strict formatting and linting rules. Before pushing code or submitting a PR, verify:

```bash
# Check linting (must include import sorting: -I)
ruff check src/ tests/ --select=E,W,F,I,N

# Check formatting
ruff format --check src/ tests/
```

To auto-format code, run:

```bash
ruff format src/ tests/
```

### 4. License Headers

Source code files should include the standard SPDX header:

```python
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan
```

### 5. Submitting a Pull Request

1. Fork the repository and create your feature branch from `main`:
   ```bash
   git checkout -b feat/my-new-feature
   ```
2. Commit your changes with clear, descriptive commit messages.
3. Ensure all tests and lint checks pass locally.
4. Push to your fork and submit a Pull Request targeting `main`.
5. If this is your first contribution, reply to the automated CLA Assistant comment on the PR to sign the CLA.
6. Once CI checks (tests, linting) and the CLA check pass, your PR will be reviewed!
