# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Claudiu Branzan

from __future__ import annotations

import io
import zipfile

from keepfor.utils.importer import ZIP_MAX_MEMBER_BYTES

DEFAULT_NOTES = {
    "project-alpha.md": (
        "---\n"
        "title: Project Alpha Overview\n"
        "tags: [project, roadmap]\n"
        "created: 2026-05-10T09:30:00Z\n"
        "---\n\n"
        "Overview of project alpha goals and milestones. #planning\n"
    ),
    "weekly-sync.md": (
        "# Weekly Team Sync\n\n"
        "Notes from the weekly sync on Monday.\n\n"
        "## Transcript\n\n"
        "Speaker 1: Welcome everyone. Let us review the roadmap.\n"
        "Speaker 2: Deployment is on schedule.\n"
    ),
    "architecture.md": (
        "# System Architecture\n\n"
        "Here is the architecture diagram:\n"
        "![diagram](../attachments/diagram.png)\n\n"
        "And here is the presentation:\n"
        "[Q3 deck](../attachments/q3-deck.pdf)\n"
    ),
    "quarterly review.md": (
        "# Quarterly Business Review\n\nQ3 metrics and performance analysis. #finance\n"
    ),
}

DEFAULT_COLLECTIONS = {
    "Projects.md": (
        "# Projects\n\n"
        "- [Project Alpha](../notes/project-alpha.md)\n"
        "- [Quarterly Review](../notes/quarterly%20review.md)\n"
    ),
    "Company Reviews.md": (
        "# Company Reviews\n\n"
        "- [[Quarterly Business Review]]\n"
        "- [Architecture Design](../notes/architecture.md)\n"
    ),
}

DEFAULT_ATTACHMENTS = {
    "diagram.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01",
    "q3-deck.pdf": b"%PDF-1.4 dummy pdf payload",
    "audio-recording.m4a": b"A" * (ZIP_MAX_MEMBER_BYTES + 1024),
}

DEFAULT_EXPORT_REPORT = (
    "# Mem Export Report\n\n"
    "The following items could not be included in your export:\n"
    "- Note 'Archived Board Deck 2021': attachment timeout\n"
)


def make_mem_export_zip(
    outer_folder: str | None = None,
    notes: dict[str, str] | None = None,
    collections: dict[str, str] | None = None,
    attachments: dict[str, bytes] | None = None,
    export_report: str | None = DEFAULT_EXPORT_REPORT,
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    """Build a realistic Mem export ZIP archive for testing."""
    root = f"{outer_folder.rstrip('/')}/" if outer_folder else ""
    notes_to_write = DEFAULT_NOTES if notes is None else notes
    cols_to_write = DEFAULT_COLLECTIONS if collections is None else collections
    atts_to_write = DEFAULT_ATTACHMENTS if attachments is None else attachments

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in notes_to_write.items():
            zf.writestr(f"{root}notes/{name}", content)

        for name, content in cols_to_write.items():
            zf.writestr(f"{root}collections/{name}", content)

        for name, payload in atts_to_write.items():
            zf.writestr(f"{root}attachments/{name}", payload)

        if export_report is not None:
            zf.writestr(f"{root}export-report.md", export_report)

        if extra_files:
            for name, payload in extra_files.items():
                zf.writestr(f"{root}{name}", payload)

    return buf.getvalue()
