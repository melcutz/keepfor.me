# Release Guide

This document defines the release process for Keepfor.me, detailing changelog maintenance, pre-release verification, and tagging procedures for maintainers.

---

## 1. Release Cadence & Versioning

Keepfor.me follows [Semantic Versioning 2.0.0](https://semver.org/):
- **Major (X.0.0)**: Breaking changes to public interfaces or `CORE_API_VERSION`.
- **Minor (1.X.0)**: New features, additive SPI extensions, non-breaking configuration additions.
- **Patch (1.1.X)**: Bug fixes, security hardening, documentation corrections.

---

## 2. Pre-Release Verification Checklist

Before tagging any release, ensure all checks pass locally:

1. **Test Suite Green**:
   ```bash
   uv run pytest tests/ -q
   ```
2. **Linting and Formatting**:
   ```bash
   uv run ruff check keepfor/ tests/ scripts/ --select=E,W,F,I,N
   uv run ruff format --check keepfor/ tests/ scripts/
   ```
3. **Packaging Verification**:
   ```bash
   uv build --wheel
   unzip -l dist/*.whl | grep -E "keepfor/(templates/base.html|migrations/0001)"
   ```
4. **Version Alignment**:
   Verify version matches across `pyproject.toml` and `keepfor/__init__.py`.
5. **Changelog Updated**:
   Ensure `CHANGELOG.md` contains an entry for the release version with all changes grouped under Keep a Changelog headings (`Breaking Changes`, `Added`, `Changed`, `Fixed`, `Upgrade Notes`).

---

## 3. Tagging Commands for Maintainers (Human)

Once the release PR is merged into `main` and CI is green:

```bash
# 1. Update local main
git checkout main
git pull origin main

# 2. Create signed tag (or use -a if GPG key is not configured)
git tag -s v1.1.0 -m "Keepfor.me 1.1.0"
# Alternative without GPG signing:
# git tag -a v1.1.0 -m "Keepfor.me 1.1.0"

# 3. Push tag to GitHub
git push origin v1.1.0
```

---

## 4. GitHub Release Creation

After pushing the tag:
1. Navigate to `https://github.com/melcutz/keepfor.me/releases/new`.
2. Select tag `v1.1.0`.
3. Set release title to `Keepfor.me 1.1.0`.
4. Copy the release notes verbatim from `CHANGELOG.md` under `## [1.1.0]`.
5. Publish release.
