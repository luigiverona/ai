from __future__ import annotations

import contextlib
import io
import unittest
from dataclasses import fields
from unittest.mock import patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.cli import InvocationKind, invocation_from_args, main, parser
from ai_setup.models import Capability, Plan, Source
from ai_setup.planning.planner import build_plan
from ai_setup.verification.checks import CheckResult


class CliPlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def invocation(self, *values: str):
        return invocation_from_args(parser(self.catalog).parse_args(values), self.catalog)

    def plan(self, *values: str):
        selection = self.invocation(*values).selection
        if selection is None:
            raise AssertionError("status has no setup selection")
        return build_plan(selection, self.catalog)

    def test_no_command_and_setup_are_identical_complete_selections(self) -> None:
        default = self.invocation()
        setup = self.invocation("setup")
        self.assertEqual(default, setup)
        self.assertEqual(default.kind, InvocationKind.SETUP)
        self.assertTrue(default.selection.complete)
        self.assertEqual(default.selection.capabilities, frozenset(Capability))

    def test_commands_map_to_internal_capabilities(self) -> None:
        expected = {
            "git": Capability.GIT,
            "github": Capability.GITHUB,
            "ssh": Capability.SSH,
            "codex": Capability.CODEX,
        }
        for command, capability in expected.items():
            with self.subTest(command=command):
                invocation = self.invocation(command)
                self.assertEqual(invocation.kind, InvocationKind.PARTIAL)
                self.assertEqual(invocation.selection.capabilities, frozenset({capability}))
        self.assertEqual(self.invocation("status").kind, InvocationKind.STATUS)
        self.assertIsNone(self.invocation("status").selection)

    def test_status_bypasses_planner_and_setup_workflow(self) -> None:
        with (
            patch("ai_setup.cli.build_plan") as planner,
            patch("ai_setup.cli.Workflow") as workflow,
            patch(
                "ai_setup.verification.readiness.ReadinessVerifier.results",
                return_value=[CheckResult("supported system", True)],
            ),
            patch("ai_setup.verification.readiness.render_readiness"),
        ):
            self.assertEqual(main(["status"]), 0)
        planner.assert_not_called()
        workflow.assert_not_called()

    def test_setup_and_focused_commands_still_use_planner(self) -> None:
        for values in (["setup", "--dry-run"], ["git", "--dry-run"]):
            with (
                self.subTest(values=values),
                patch("ai_setup.cli.build_plan", wraps=build_plan) as planner,
                patch("ai_setup.cli.Workflow.run", return_value=0),
            ):
                self.assertEqual(main(values), 0)
                planner.assert_called_once()

    def test_status_is_not_a_capability(self) -> None:
        self.assertNotIn("check", {capability.value for capability in Capability})

    def test_apps_categories_are_positional_deduplicated_and_order_independent(self) -> None:
        all_apps = self.invocation("apps")
        self.assertEqual(all_apps.selection.capabilities, {Capability.APPS})
        self.assertFalse(all_apps.selection.app_categories)
        first = self.plan("apps", "vpn", "browser", "vpn")
        second = self.plan("apps", "browser", "vpn")
        self.assertEqual(first, second)
        categories = {p.category for p in first.packages if p.source in {Source.PACMAN, Source.AUR}}
        self.assertTrue({"browser", "vpn"}.issubset(categories))

    def test_unknown_category_is_actionable_and_sorted(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown application category 'office'") as caught:
            self.invocation("apps", "office")
        valid = ", ".join(sorted(self.catalog.app_categories))
        self.assertIn(f"choose from: {valid}", str(caught.exception))

    def test_prerequisites_remain_planner_owned(self) -> None:
        github = self.plan("github")
        self.assertIn(Capability.GIT, github.prerequisites)
        self.assertEqual({p.identifier for p in github.packages}, {"git", "github-cli", "openssh"})
        ssh = self.plan("ssh")
        self.assertIn(Capability.GITHUB, ssh.prerequisites)
        codex = self.plan("codex")
        self.assertIn(Capability.SHELL, codex.prerequisites)
        self.assertEqual({p.identifier for p in codex.packages}, {"codex", "curl"})
        game = self.plan("apps", "game")
        self.assertIn(Capability.FLATPAK, game.prerequisites)
        self.assertIn(Capability.FLATHUB, game.prerequisites)

    def test_plan_contains_only_runtime_consumed_state(self) -> None:
        self.assertEqual(
            [field.name for field in fields(Plan)],
            ["selected", "prerequisites", "packages"],
        )

    def test_options_work_before_and_after_commands_without_overwrite(self) -> None:
        cli = parser(self.catalog)
        for before, after in (
            (("--dry-run", "setup"), ("setup", "--dry-run")),
            (("-n", "apps", "browser"), ("apps", "browser", "--dry-run")),
            (("-v", "github"), ("github", "--verbose")),
            (("--verbose", "status"), ("status", "--verbose")),
            (("-y", "git"), ("git", "--yes")),
            (("--keep-temp", "codex"), ("codex", "--keep-temp")),
        ):
            left, right = cli.parse_args(before), cli.parse_args(after)
            self.assertEqual(vars(left), vars(right))

    def test_root_help_is_user_oriented(self) -> None:
        help_text = parser(self.catalog).format_help()
        for text in (
            "usage: ai [command] [options]",
            "Set up an Arch Linux workstation.",
            "setup",
            "apps [CATEGORY ...]",
            "git",
            "github",
            "ssh",
            "codex",
            "status",
            "-n, --dry-run",
            "-y, --yes",
            "-v, --verbose",
            "--version",
        ):
            self.assertIn(text, help_text)
        for removed in (
            "--deps",
            "--flatpak",
            "--flathub",
            "--check",
            "--app",
            "--keep-temp",
            "select ",
        ):
            self.assertNotIn(removed, help_text)

    def test_command_help_is_focused(self) -> None:
        cli = parser(self.catalog)
        choices = cli._subparsers._group_actions[0].choices  # type: ignore[union-attr]
        expected = {
            "setup": "usage: ai setup [options]",
            "apps": "usage: ai apps [CATEGORY ...] [options]",
            "git": "usage: ai git [options]",
            "github": "usage: ai github [options]",
            "ssh": "usage: ai ssh [options]",
            "codex": "usage: ai codex [options]",
            "status": "usage: ai status [options]",
        }
        for command, usage in expected.items():
            with self.subTest(command=command):
                self.assertTrue(choices[command].format_help().startswith(usage))
        apps = choices["apps"].format_help()
        self.assertIn("Available:", apps)
        self.assertNotIn("dependency", apps.lower())

    def test_invalid_option_stops_before_workflow(self) -> None:
        with (
            patch("ai_setup.cli.Workflow.run") as run,
            patch("ai_setup.cli.StatusWorkflow.run") as status_run,
        ):
            error = io.StringIO()
            with contextlib.redirect_stderr(error), self.assertRaises(SystemExit) as caught:
                main(["--invalid-option"])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("unrecognized arguments: --invalid-option", error.getvalue())
            run.assert_not_called()
            status_run.assert_not_called()

    def test_version(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            parser(self.catalog).parse_args(("--version",))
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue(), "ai 2.0.0\n")
