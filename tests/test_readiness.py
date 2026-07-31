from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.cli import invocation_from_args, parser
from ai_setup.execution.runner import CommandRunner
from ai_setup.models import Capability, RunOptions, Selection
from ai_setup.planning.planner import build_plan
from ai_setup.status import StatusWorkflow
from ai_setup.ui.terminal import Terminal
from ai_setup.verification import readiness
from ai_setup.verification.checks import CheckResult, Verifier
from ai_setup.workflow import Workflow


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_complete_result_order_matches_existing_contract(self) -> None:
        scope = readiness.ReadinessScope.complete(self.catalog)
        package_results = [CheckResult(package.name, True) for package in scope.packages]
        with (
            patch.object(Verifier, "system", return_value=CheckResult("supported system", True)),
            patch.object(Verifier, "package", side_effect=package_results),
            patch.object(Verifier, "flathub", return_value=CheckResult("Flathub remote", True)),
            patch("ai_setup.verification.readiness.GitConfigurator.verify", return_value=True),
            patch(
                "ai_setup.verification.readiness.GitHubConfigurator.authenticated",
                return_value=True,
            ),
            patch(
                "ai_setup.verification.readiness.GitHubConfigurator.protocol", return_value="ssh"
            ),
            patch("ai_setup.verification.readiness.SSHManager.verify", return_value=True),
            patch("ai_setup.verification.readiness.CodexManager.verified", return_value=True),
            patch(
                "ai_setup.verification.readiness.CodexManager.profiles_distinct", return_value=True
            ),
            patch.object(
                Verifier,
                "shell_configuration",
                return_value=CheckResult("shell PATH configuration", True),
            ),
        ):
            results = readiness.ReadinessVerifier(
                scope,
                CommandRunner(),
                Path("/tmp/readiness"),
                target_shell=Mock(),
            ).results(read_only=True)
        self.assertEqual(
            [result.name for result in results],
            [
                "supported system",
                *(package.name for package in scope.packages),
                "Flathub remote",
                "Git identity",
                "GitHub authentication",
                "GitHub SSH protocol",
                "SSH connection",
                "codex-01",
                "codex-02",
                "Codex profile isolation",
                "shell PATH configuration",
            ],
        )

    def test_complete_success_renderer_preserves_grouped_output(self) -> None:
        lines: list[str] = []
        readiness.render_readiness(
            readiness.ReadinessScope.complete(self.catalog),
            Terminal(output=lines.append),
        )
        self.assertEqual(
            lines,
            [
                "All software requirements are ready.",
                "The Git identity is ready.",
                "GitHub authentication is ready.",
                "The SSH connection is ready.",
                "Both Codex profiles are ready.",
                "The shell PATH is ready.",
            ],
        )

    def test_one_state_inspector_is_reused_for_all_packages_in_a_run(self) -> None:
        packages = tuple(
            package
            for package in self.catalog.deps
            if package.identifier in {"curl", "git", "openssh"}
        )
        scope = readiness.ReadinessScope(frozenset(), packages)
        with (
            patch("ai_setup.verification.readiness.StateInspector") as factory,
            patch.object(Verifier, "system", return_value=CheckResult("supported system", True)),
        ):
            factory.return_value.package_installed.return_value = True
            results = readiness.ReadinessVerifier(
                scope, CommandRunner(), Path("/tmp/readiness")
            ).results()
        factory.assert_called_once()
        self.assertEqual(
            factory.return_value.package_installed.call_args_list,
            [call(package) for package in packages],
        )
        self.assertTrue(all(result.passed for result in results))

    def test_separate_readiness_runs_reinspect_current_package_state(self) -> None:
        package = next(package for package in self.catalog.deps if package.identifier == "git")
        scope = readiness.ReadinessScope(frozenset(), (package,))
        absent = Mock()
        absent.package_installed.return_value = False
        present = Mock()
        present.package_installed.return_value = True
        with (
            patch(
                "ai_setup.verification.readiness.StateInspector",
                side_effect=[absent, present],
            ) as factory,
            patch.object(Verifier, "system", return_value=CheckResult("supported system", True)),
        ):
            before = readiness.ReadinessVerifier(
                scope, CommandRunner(), Path("/tmp/readiness")
            ).results()
            after = readiness.ReadinessVerifier(
                scope, CommandRunner(), Path("/tmp/readiness")
            ).results()
        self.assertFalse(before[1].passed)
        self.assertTrue(after[1].passed)
        self.assertEqual(factory.call_count, 2)

    def test_setup_and_status_share_verifier_results_and_renderer(self) -> None:
        plan = build_plan(Selection(frozenset(Capability), complete=True), self.catalog)
        expected = [
            CheckResult("supported system", True),
            CheckResult("Git", True),
        ]
        calls: list[tuple[readiness.ReadinessScope, bool]] = []

        class RecordingVerifier:
            def __init__(
                self,
                scope: readiness.ReadinessScope,
                _runner: object,
                _home: Path,
                **_kwargs: object,
            ) -> None:
                self.scope = scope

            def results(self, *, read_only: bool = False) -> list[CheckResult]:
                calls.append((self.scope, read_only))
                return list(expected)

        setup_lines: list[str] = []
        status_lines: list[str] = []
        with (
            patch(
                "ai_setup.verification.readiness.ReadinessVerifier",
                RecordingVerifier,
            ),
            patch("ai_setup.verification.readiness.render_readiness") as renderer,
        ):
            Workflow(
                plan,
                RunOptions(home=Path("/tmp/setup")),
                Terminal(output=setup_lines.append),
            )._verify()
            status = StatusWorkflow(
                self.catalog,
                RunOptions(home=Path("/tmp/status")),
                Terminal(output=status_lines.append),
            ).run()
        self.assertEqual(status, 0)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[0][1:], (False,))
        self.assertEqual(calls[1][1:], (True,))
        self.assertEqual(renderer.call_count, 2)
        self.assertEqual(renderer.call_args_list[0].args[0], renderer.call_args_list[1].args[0])
        self.assertIn("All verification checks passed.", setup_lines)
        self.assertEqual(status_lines[-1], "Workstation ready.")

    def test_focused_and_complete_readiness_scopes_are_preserved(self) -> None:
        expected = {
            (): (
                frozenset(Capability),
                {
                    "base-devel",
                    "codex",
                    "curl",
                    "discord",
                    "flatpak",
                    "git",
                    "github-cli",
                    "librewolf-bin",
                    "mullvad-browser-bin",
                    "mullvad-vpn",
                    "openssh",
                    "org.vinegarhq.Sober",
                    "spotify-launcher",
                    "visual-studio-code-bin",
                    "yay-bin",
                },
            ),
            ("apps",): (
                frozenset(
                    {
                        Capability.APPS,
                        Capability.DEPS,
                        Capability.FLATPAK,
                        Capability.FLATHUB,
                        Capability.CODEX,
                        Capability.SHELL,
                    }
                ),
                {
                    "base-devel",
                    "codex",
                    "curl",
                    "discord",
                    "flatpak",
                    "git",
                    "librewolf-bin",
                    "mullvad-browser-bin",
                    "mullvad-vpn",
                    "org.vinegarhq.Sober",
                    "spotify-launcher",
                    "visual-studio-code-bin",
                    "yay-bin",
                },
            ),
            ("git",): (frozenset({Capability.GIT}), set()),
            ("github",): (
                frozenset({Capability.GITHUB, Capability.DEPS, Capability.GIT}),
                {"git", "github-cli", "openssh"},
            ),
            ("ssh",): (
                frozenset(
                    {
                        Capability.SSH,
                        Capability.DEPS,
                        Capability.GIT,
                        Capability.GITHUB,
                    }
                ),
                {"git", "github-cli", "openssh"},
            ),
            ("codex",): (
                frozenset({Capability.CODEX, Capability.DEPS, Capability.SHELL}),
                {"codex", "curl"},
            ),
        }
        cli_parser = parser(self.catalog)
        for values, (capabilities, package_ids) in expected.items():
            with self.subTest(values=values):
                invocation = invocation_from_args(cli_parser.parse_args(values), self.catalog)
                if invocation.selection is None:
                    self.fail("setup command unexpectedly lacked a selection")
                scope = readiness.ReadinessScope.from_plan(
                    build_plan(invocation.selection, self.catalog)
                )
                self.assertEqual(scope.capabilities, capabilities)
                self.assertEqual(
                    {package.identifier for package in scope.packages},
                    package_ids,
                )
        status_scope = readiness.ReadinessScope.complete(self.catalog)
        self.assertEqual(status_scope.capabilities, frozenset(Capability))
        self.assertEqual(
            {package.identifier for package in status_scope.packages},
            {package.identifier for package in (*self.catalog.apps, *self.catalog.deps)},
        )
