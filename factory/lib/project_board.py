"""GitHub ProjectV2 ("Factory") board creation and automatic stage-to-column sync."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable

DEFAULT_BOARD_TITLE = "Factory"
DEFAULT_BOARD_DESCRIPTION = "Watch the Needs Human column; the factory moves everything else."

DEFAULT_STATUS_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("Inbox", "GRAY", "Newly filed issues awaiting triage"),
    ("Ready", "GREEN", "Queued with the factory label"),
    ("Building", "BLUE", "Actively being executed by the factory"),
    ("Review", "PURPLE", "PR ready for human review or merge"),
    ("Done", "GREEN", "Merged to base branch"),
    ("Deployed", "TEAL", "Deployed and verified"),
    ("Skipped", "GRAY", "Rejected or skipped by the factory"),
    ("Needs Human", "RED", "Paused for human input or intervention"),
)

STAGE_TO_BOARD_STATUS: dict[str, str] = {
    "queued": "Ready",
    "intake": "Building",
    "spec": "Building",
    "build": "Building",
    "gate": "Building",
    "review": "Building",
    "approve": "Building",
    "merging": "Building",
    "staging_qa": "Building",
    "ready": "Review",
    "merged": "Done",
    "needs-human": "Needs Human",
    "needs-info": "Needs Human",
    "rejected": "Skipped",
}

_UPDATE_FIELD_MUTATION = """
mutation($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
  updateProjectV2Field(input: {
    fieldId: $fieldId,
    singleSelectOptions: $options
  }) {
    projectV2Field {
      ... on ProjectV2SingleSelectField {
        id
        options { id name }
      }
    }
  }
}
""".strip()

_UPDATE_DESC_MUTATION = """
mutation($projectId: ID!, $desc: String!) {
  updateProjectV2(input: {projectId: $projectId, shortDescription: $desc}) {
    projectV2 { id }
  }
}
""".strip()


def _default_gh_runner(args: list[str], cwd: str = ".") -> str:
    res = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (exit {res.returncode}): {res.stderr.strip()}")
    return res.stdout


def create_factory_board(
    owner: str = "@me",
    title: str = DEFAULT_BOARD_TITLE,
    *,
    runner: Callable[[list[str], str], str] | None = None,
    cwd: str = ".",
) -> dict:
    """Create a generic Factory GitHub ProjectV2 board if one does not already exist."""
    run = runner or _default_gh_runner
    listed = json.loads(run(["project", "list", "--owner", owner, "--format", "json"], cwd) or "{}")
    for proj in listed.get("projects") or []:
        if str(proj.get("title") or "").strip().lower() == title.strip().lower():
            return {"created": False, "project": proj}

    created = json.loads(run(["project", "create", "--owner", owner, "--title", title, "--format", "json"], cwd))
    proj_id = created["id"]
    proj_num = str(created["number"])
    proj_owner = (created.get("owner") or {}).get("login") or owner

    run(
        [
            "api",
            "graphql",
            "-f",
            f"query={_UPDATE_DESC_MUTATION}",
            "-f",
            f"projectId={proj_id}",
            "-f",
            f"desc={DEFAULT_BOARD_DESCRIPTION}",
        ],
        cwd,
    )

    fields_data = json.loads(run(["project", "field-list", proj_num, "--owner", proj_owner, "--format", "json"], cwd))
    fields = fields_data.get("fields") or []
    status_field = next((f for f in fields if f.get("name") == "Status"), None)
    if status_field:
        options_payload = [
            {"name": name, "color": color, "description": desc}
            for name, color, desc in DEFAULT_STATUS_OPTIONS
        ]
        run(
            [
                "api",
                "graphql",
                "-f",
                f"query={_UPDATE_FIELD_MUTATION}",
                "-f",
                f"fieldId={status_field['id']}",
                "--raw-field",
                f"options={json.dumps(options_payload)}",
            ],
            cwd,
        )

    if not any(f.get("name") == "Priority" for f in fields):
        run(
            [
                "project",
                "field-create",
                proj_num,
                "--owner",
                proj_owner,
                "--name",
                "Priority",
                "--data-type",
                "SINGLE_SELECT",
                "--single-select-options",
                "P0,P1,P2",
            ],
            cwd,
        )

    return {"created": True, "project": created}


class ProjectBoardSync:
    """Best-effort sync of factory job stages onto a user's 'Factory' GitHub ProjectV2."""

    def __init__(
        self,
        project_id: str,
        project_number: int,
        owner: str,
        status_field_id: str,
        options_by_name: dict[str, str],
        *,
        runner: Callable[[list[str], str], str] | None = None,
        cwd: str = ".",
    ):
        self.project_id = project_id
        self.project_number = project_number
        self.owner = owner
        self.status_field_id = status_field_id
        self.options_by_name = {k.strip().lower(): v for k, v in options_by_name.items()}
        self._runner = runner or _default_gh_runner
        self._cwd = cwd
        self._lock = threading.Lock()
        self._item_ids: dict[str, str] = {}
        self._last_column: dict[str, str] = {}

    @classmethod
    def discover(
        cls,
        repo_root: str = ".",
        *,
        title: str = DEFAULT_BOARD_TITLE,
        owner: str = "@me",
        runner: Callable[[list[str], str], str] | None = None,
    ) -> ProjectBoardSync | None:
        """Discover a matching GitHub ProjectV2 if present; return None if unavailable."""
        run = runner or _default_gh_runner
        try:
            listed = json.loads(run(["project", "list", "--owner", owner, "--format", "json"], repo_root) or "{}")
            proj = next(
                (
                    p
                    for p in (listed.get("projects") or [])
                    if str(p.get("title") or "").strip().lower() == title.strip().lower() and not p.get("closed")
                ),
                None,
            )
            if not proj:
                return None
            proj_owner = (proj.get("owner") or {}).get("login") or owner
            proj_num = int(proj["number"])
            fields_data = json.loads(
                run(["project", "field-list", str(proj_num), "--owner", proj_owner, "--format", "json"], repo_root)
                or "{}"
            )
            status_field = next((f for f in (fields_data.get("fields") or []) if f.get("name") == "Status"), None)
            if not status_field:
                return None
            options = {
                str(opt["name"]).strip().lower(): str(opt["id"])
                for opt in (status_field.get("options") or [])
                if opt.get("name") and opt.get("id")
            }
            return cls(
                project_id=str(proj["id"]),
                project_number=proj_num,
                owner=proj_owner,
                status_field_id=str(status_field["id"]),
                options_by_name=options,
                runner=run,
                cwd=repo_root,
            )
        except Exception:
            return None

    def _resolve_option_id(self, column_name: str) -> str | None:
        return self.options_by_name.get(column_name.strip().lower())

    def sync_stage(self, issue_url: str, stage: str) -> bool:
        """Add issue to the project board if needed and move it to the column for `stage`."""
        if not issue_url:
            return False
        target_col = STAGE_TO_BOARD_STATUS.get(stage)
        if not target_col:
            return False
        option_id = self._resolve_option_id(target_col)
        if not option_id:
            return False

        with self._lock:
            if self._last_column.get(issue_url) == target_col:
                return True
            try:
                item_id = self._item_ids.get(issue_url)
                if not item_id:
                    added = json.loads(
                        self._runner(
                            [
                                "project",
                                "item-add",
                                str(self.project_number),
                                "--owner",
                                self.owner,
                                "--url",
                                issue_url,
                                "--format",
                                "json",
                            ],
                            self._cwd,
                        )
                    )
                    item_id = str(added["id"])
                    self._item_ids[issue_url] = item_id

                self._runner(
                    [
                        "project",
                        "item-edit",
                        "--id",
                        item_id,
                        "--field-id",
                        self.status_field_id,
                        "--project-id",
                        self.project_id,
                        "--single-select-option-id",
                        option_id,
                    ],
                    self._cwd,
                )
                self._last_column[issue_url] = target_col
                return True
            except Exception as exc:
                print(f"[factory] warning: project board sync failed for {issue_url}: {exc}", file=sys.stderr)
                return False
