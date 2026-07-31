from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_setup import workflow as workflow_module
from ai_setup.config import codex as codex_config
from ai_setup.config.codex import CODEX_PROFILES, CodexManager
from ai_setup.config.github import (
    AUTH_LOGIN_ARGV,
    AUTH_STATUS_ARGV,
    PROTOCOL_GET_ARGV,
    PROTOCOL_SET_ARGV,
    GitHubConfigurator,
)
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import Command, CommandResult
from ai_setup.models import (
    STAGE_SPECS,
    Capability,
    Plan,
    RunOptions,
    WorkflowProgress,
    WorkflowStage,
    stage_spec,
)
from ai_setup.packages.managers import FlatpakManager, flathub_readiness
from ai_setup.ui.terminal import Terminal
from ai_setup.verification import readiness
from ai_setup.verification.checks import Verifier
from ai_setup.workflow import Workflow
from tests.helpers import FakeRunner


class RuntimeDefinitionTests(unittest.TestCase):
    def test_stage_specification_is_complete_ordered_and_unique(self) -> None:
        stages = tuple(spec.stage for spec in STAGE_SPECS)
        self.assertEqual(stages, tuple(WorkflowStage))
        self.assertEqual(len(stages), len(set(stages)))
        self.assertIs(stages[-1], WorkflowStage.VERIFICATION)
        self.assertTrue(all(stage_spec(stage).stage is stage for stage in WorkflowStage))
        self.assertTrue(
            all(
                spec.plan_label and spec.resume_label and spec.interruption_label
                for spec in STAGE_SPECS
            )
        )
        expected_capabilities = {
            WorkflowStage.ADMINISTRATOR: {Capability.SYSTEM},
            WorkflowStage.SYSTEM: {Capability.SYSTEM},
            WorkflowStage.APPLICATIONS: {Capability.APPS},
            WorkflowStage.FLATPAK: {Capability.FLATPAK, Capability.FLATHUB},
            WorkflowStage.GIT: {Capability.GIT},
            WorkflowStage.GITHUB: {Capability.GITHUB},
            WorkflowStage.SSH: {Capability.SSH},
            WorkflowStage.CODEX: {Capability.CODEX},
            WorkflowStage.SHELL: {Capability.SHELL},
            WorkflowStage.VERIFICATION: set(),
        }
        self.assertEqual(
            {spec.stage: set(spec.capabilities) for spec in STAGE_SPECS},
            expected_capabilities,
        )
        self.assertEqual(
            {spec.stage for spec in STAGE_SPECS if spec.include_when_native_packages_pending},
            {WorkflowStage.ADMINISTRATOR, WorkflowStage.APPLICATIONS},
        )
        self.assertEqual(
            {spec.stage for spec in STAGE_SPECS if spec.always},
            {WorkflowStage.VERIFICATION},
        )

    def test_plan_and_interruption_render_from_stage_specification(self) -> None:
        lines: list[str] = []
        workflow = Workflow(
            Plan(tuple(Capability), (), ()),
            RunOptions(dry_run=True),
            Terminal(output=lines.append),
        )
        workflow.progress = WorkflowProgress(tuple(WorkflowStage))
        workflow._render_plan(())
        self.assertEqual(
            lines[1 : 1 + len(STAGE_SPECS)],
            [f"{spec.plan_label}." for spec in STAGE_SPECS],
        )
        for spec in STAGE_SPECS:
            with self.subTest(stage=spec.stage):
                interrupted: list[str] = []
                workflow.terminal = Terminal(output=interrupted.append)
                workflow.progress = WorkflowProgress((spec.stage,), current=spec.stage)
                workflow._render_interruption()
                self.assertIn(
                    f"Setup paused during {spec.interruption_label}.",
                    interrupted,
                )

    def test_codex_profile_specification_drives_distinct_paths(self) -> None:
        self.assertIs(workflow_module.CODEX_PROFILES, codex_config.CODEX_PROFILES)
        self.assertIs(readiness.CODEX_PROFILES, codex_config.CODEX_PROFILES)
        self.assertEqual(
            [
                (
                    profile.identifier,
                    profile.launcher_name,
                    profile.directory_name,
                    profile.display_label,
                )
                for profile in CODEX_PROFILES
            ],
            [
                ("01", "codex-01", "01", "codex-01"),
                ("02", "codex-02", "02", "codex-02"),
            ],
        )
        with tempfile.TemporaryDirectory() as raw:
            manager = CodexManager(FakeRunner(), Path(raw))  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            roots = [manager.state_root / spec.directory_name for spec in CODEX_PROFILES]
            launchers = [manager.bin_dir / spec.launcher_name for spec in CODEX_PROFILES]
            self.assertEqual(len(set(roots)), 2)
            self.assertEqual(len(set(launchers)), 2)
            self.assertTrue(all(path.is_dir() for path in roots))
            self.assertTrue(all(path.is_file() for path in launchers))
            self.assertTrue(manager.profiles_distinct())
            self.assertTrue(all(str(manager.shared_bin) in path.read_text() for path in launchers))

    def test_flathub_ready_setup_has_one_precheck_and_no_remote_mutation(self) -> None:
        remotes = ("flatpak", "remotes", "--user", "--columns=name")
        runner = FakeRunner({remotes: CommandResult(remotes, 0, "flathub\n", "")})
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n")
            lines: list[str] = []
            workflow = Workflow(
                Plan((Capability.FLATHUB,), (), ()),
                RunOptions(home=home),
                Terminal(output=lines.append),
                runner=runner,  # type: ignore[arg-type]
            )
            inspector = Mock()
            inspector.pending.return_value = ()
            with patch("ai_setup.packages.managers.shutil.which", return_value="/usr/bin/flatpak"):
                workflow._flatpak(inspector)
        self.assertEqual([command.argv for command in runner.commands].count(remotes), 1)
        self.assertFalse(
            any(command.argv[:2] == ("flatpak", "remote-add") for command in runner.commands)
        )
        self.assertIn("Flathub is already configured.", lines)

    def test_flathub_missing_setup_adds_once_and_readiness_reinspects(self) -> None:
        remotes = ("flatpak", "remotes", "--user", "--columns=name")

        class FlathubRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.configured = False

            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                if command.argv == remotes:
                    output = "flathub\n" if self.configured else ""
                    return CommandResult(command.argv, 0, output, "")
                if command.argv[:2] == ("flatpak", "remote-add"):
                    self.configured = True
                return CommandResult(command.argv, 0, "", "")

        runner = FlathubRunner()
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n")
            with patch("ai_setup.packages.managers.shutil.which", return_value="/usr/bin/flatpak"):
                before = flathub_readiness(runner, home)  # type: ignore[arg-type]
                changed = FlatpakManager(runner, home).ensure_flathub(before)  # type: ignore[arg-type]
                self.assertTrue(changed)
                self.assertEqual([command.argv for command in runner.commands].count(remotes), 1)
                remote_add = [
                    command.argv
                    for command in runner.commands
                    if command.argv[:2] == ("flatpak", "remote-add")
                ]
                self.assertEqual(
                    remote_add,
                    [
                        (
                            "flatpak",
                            "remote-add",
                            "--user",
                            "--if-not-exists",
                            "flathub",
                            "https://dl.flathub.org/repo/flathub.flatpakrepo",
                        )
                    ],
                )
                result = Verifier(runner, home).flathub()  # type: ignore[arg-type]
                self.assertTrue(result.passed)
                self.assertEqual([command.argv for command in runner.commands].count(remotes), 2)

    def test_missing_flatpak_is_structured_and_never_attempts_remote_add(self) -> None:
        class MissingRunner(FakeRunner):
            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                raise FileNotFoundError(command.argv[0])

        runner = MissingRunner()
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            with patch("ai_setup.packages.managers.shutil.which", return_value=None):
                readiness = flathub_readiness(runner, home)  # type: ignore[arg-type]
                self.assertFalse(readiness.flatpak_available)
                with self.assertRaisesRegex(ValidationError, "Flatpak is not installed"):
                    FlatpakManager(runner, home).ensure_flathub(readiness)  # type: ignore[arg-type]
                self.assertFalse(
                    any(
                        command.argv[:2] == ("flatpak", "remote-add") for command in runner.commands
                    )
                )
                result = Verifier(runner, home).flathub()  # type: ignore[arg-type]
                self.assertFalse(result.passed)
                self.assertEqual(result.reason, "Flatpak is not installed")

    def test_github_ready_path_uses_two_read_only_probes_and_no_mutation(self) -> None:
        runner = FakeRunner(
            {
                AUTH_STATUS_ARGV: CommandResult(AUTH_STATUS_ARGV, 0, "", ""),
                PROTOCOL_GET_ARGV: CommandResult(PROTOCOL_GET_ARGV, 0, "ssh\n", ""),
                ("gh", "api", "user", "--jq", ".login"): CommandResult((), 0, "luigiverona\n", ""),
            }
        )
        lines: list[str] = []
        workflow = Workflow(
            Plan((Capability.GITHUB,), (), ()),
            RunOptions(),
            Terminal(output=lines.append),
            runner=runner,  # type: ignore[arg-type]
        )
        workflow._github()
        argv = [command.argv for command in runner.commands]
        self.assertEqual(argv.count(AUTH_STATUS_ARGV), 1)
        self.assertEqual(argv.count(PROTOCOL_GET_ARGV), 1)
        self.assertNotIn(AUTH_LOGIN_ARGV, argv)
        self.assertNotIn(PROTOCOL_SET_ARGV, argv)
        self.assertEqual(
            lines,
            ["Already signed in as luigiverona.", "Git protocol already uses SSH."],
        )

    def test_github_protocol_mutation_is_reverified_without_cached_state(self) -> None:
        class ProtocolRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.protocol = "https"

            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                if command.argv == PROTOCOL_GET_ARGV:
                    return CommandResult(command.argv, 0, self.protocol + "\n", "")
                if command.argv == PROTOCOL_SET_ARGV:
                    self.protocol = "ssh"
                return CommandResult(command.argv, 0, "", "")

        runner = ProtocolRunner()
        configurator = GitHubConfigurator(runner)  # type: ignore[arg-type]
        before = configurator.protocol()
        configurator.authenticate(authenticated=True, protocol=before)
        argv = [command.argv for command in runner.commands]
        self.assertEqual(argv, [PROTOCOL_GET_ARGV, PROTOCOL_SET_ARGV, PROTOCOL_GET_ARGV])


if __name__ == "__main__":
    unittest.main()
