"""Unit tests for generic Factory GitHub ProjectV2 board creation and stage sync."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

from factory.lib.project_board import (
    DEFAULT_BOARD_DESCRIPTION,
    DEFAULT_STATUS_OPTIONS,
    ProjectBoardSync,
    create_factory_board,
)


def _load_cli_module():
    cli_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "factory", "bin", "factory")
    )
    loader = importlib.machinery.SourceFileLoader("factory_cli_board_mod", cli_path)
    spec = importlib.util.spec_from_loader("factory_cli_board_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ProjectBoardTests(unittest.TestCase):
    def test_create_factory_board_creates_generic_columns_and_priority(self):
        calls = []

        def fake_runner(args, _cwd):
            calls.append(args)
            if args[:2] == ["project", "list"]:
                return json.dumps({"projects": []})
            if args[:2] == ["project", "create"]:
                return json.dumps(
                    {
                        "id": "PVT_123",
                        "number": 7,
                        "title": "Factory",
                        "url": "https://github.com/users/demo/projects/7",
                        "owner": {"login": "demo"},
                    }
                )
            if args[:2] == ["project", "field-list"]:
                return json.dumps(
                    {
                        "fields": [
                            {"id": "F_TITLE", "name": "Title"},
                            {"id": "F_STATUS", "name": "Status", "options": []},
                        ]
                    }
                )
            return "{}"

        res = create_factory_board(owner="@me", title="Factory", runner=fake_runner)
        self.assertTrue(res["created"])
        self.assertEqual(res["project"]["number"], 7)

        # Verify description, generic Status options (Needs Human, no
        # personal/product names), and Priority
        graphql_calls = [c for c in calls if c[:2] == ["api", "graphql"]]
        self.assertEqual(len(graphql_calls), 2)
        self.assertIn(f"desc={DEFAULT_BOARD_DESCRIPTION}", graphql_calls[0])
        options_arg = next(a for a in graphql_calls[1] if a.startswith("options="))
        options_data = json.loads(options_arg.split("=", 1)[1])
        self.assertEqual(
            [o["name"] for o in options_data], [o[0] for o in DEFAULT_STATUS_OPTIONS]
        )
        self.assertIn("Needs Human", [o["name"] for o in options_data])

        field_create = next(c for c in calls if c[:2] == ["project", "field-create"])
        self.assertIn("Priority", field_create)
        self.assertIn("P0,P1,P2", field_create)

    def test_create_factory_board_is_idempotent_when_board_exists(self):
        calls = []

        def fake_runner(args, _cwd):
            calls.append(args)
            if args[:2] == ["project", "list"]:
                return json.dumps(
                    {
                        "projects": [
                            {
                                "id": "PVT_99",
                                "number": 2,
                                "title": "Factory",
                                "url": "https://github.com/users/demo/projects/2",
                            }
                        ]
                    }
                )
            raise AssertionError(f"Unexpected call: {args}")

        res = create_factory_board(runner=fake_runner)
        self.assertFalse(res["created"])
        self.assertEqual(res["project"]["id"], "PVT_99")
        self.assertEqual(len(calls), 1)

    def test_discover_returns_none_when_missing_or_gh_fails(self):
        def failing_runner(_args, _cwd):
            raise RuntimeError("missing project scope")

        self.assertIsNone(ProjectBoardSync.discover(".", runner=failing_runner))

        def empty_runner(_args, _cwd):
            return json.dumps(
                {
                    "projects": [
                        {"id": "PVT_1", "number": 1, "title": "Other", "closed": False}
                    ]
                }
            )

        self.assertIsNone(ProjectBoardSync.discover(".", runner=empty_runner))

    def test_sync_stage_maps_columns_deduplicates_and_supports_legacy_needs_column(
        self,
    ):
        calls = []

        def fake_runner(args, _cwd):
            calls.append(args)
            if args[:2] == ["project", "item-add"]:
                return json.dumps({"id": "ITEM_42"})
            return "{}"

        board = ProjectBoardSync(
            project_id="PVT_2",
            project_number=2,
            owner="melcutz",
            status_field_id="F_STATUS",
            options_by_name={
                "Ready": "OPT_READY",
                "Building": "OPT_BUILD",
                "Review": "OPT_REVIEW",
                "Done": "OPT_DONE",
                "Skipped": "OPT_SKIP",
                "Needs Human": "OPT_NEEDS_HUMAN",
            },
            runner=fake_runner,
        )

        url = "https://github.com/melcutz/repo/issues/42"
        self.assertTrue(board.sync_stage(url, "queued"))
        self.assertTrue(board.sync_stage(url, "intake"))
        # Subsequent active stages map to Building -> cached, no extra gh call!
        self.assertTrue(board.sync_stage(url, "build"))
        self.assertTrue(board.sync_stage(url, "review"))
        self.assertTrue(board.sync_stage(url, "ready"))
        self.assertTrue(board.sync_stage(url, "merged"))
        self.assertTrue(board.sync_stage(url, "needs-human"))

        add_calls = [c for c in calls if c[:2] == ["project", "item-add"]]
        edit_calls = [c for c in calls if c[:2] == ["project", "item-edit"]]
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(
            [c[c.index("--single-select-option-id") + 1] for c in edit_calls],
            ["OPT_READY", "OPT_BUILD", "OPT_REVIEW", "OPT_DONE", "OPT_NEEDS_HUMAN"],
        )

    def test_cli_init_board_invokes_create_factory_board(self):
        cli = _load_cli_module()
        with patch.object(
            cli,
            "create_factory_board",
            return_value={
                "created": True,
                "project": {"title": "Factory", "url": "https://example.test/p/1"},
            },
        ) as mock_create:
            self.assertEqual(
                cli.main(["init-board", "--owner", "octocat", "--title", "Factory"]), 0
            )
            mock_create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
