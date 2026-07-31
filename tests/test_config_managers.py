from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_setup.config.codex import CodexManager
from ai_setup.config.github import GitHubConfigurator
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import Command, CommandResult, manual_authentication_env
from ai_setup.models import STAGE_ORDER, WorkflowStage
from tests.helpers import FakeRunner


class GitHubAuthenticationTests(unittest.TestCase):
    def test_login_is_exact_interactive_argv_and_is_verified_before_protocol(self) -> None:
        class Runner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.signed_in = False
                self.protocol = "https"

            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                if command.argv[:3] == ("gh", "auth", "login"):
                    self.signed_in = True
                if command.argv[:3] == ("gh", "auth", "status"):
                    return CommandResult(command.argv, 0 if self.signed_in else 1, "", "")
                if command.argv[:3] == ("gh", "config", "set"):
                    self.protocol = "ssh"
                if command.argv[:3] == ("gh", "config", "get"):
                    return CommandResult(command.argv, 0, self.protocol + "\n", "")
                return CommandResult(command.argv, 0, "", "")

        runner = Runner()
        with patch("ai_setup.config.github.interactive_terminal_available", return_value=True):
            GitHubConfigurator(runner).authenticate(authenticated=False)  # type: ignore[arg-type]
        login = runner.commands[0]
        self.assertEqual(
            login.argv,
            (
                "gh",
                "auth",
                "login",
                "--hostname",
                "github.com",
                "--web",
                "--git-protocol",
                "ssh",
                "--skip-ssh-key",
            ),
        )
        self.assertTrue(login.interactive)
        self.assertEqual(login.env, manual_authentication_env())
        self.assertEqual(
            [command.argv[:3] for command in runner.commands],
            [
                ("gh", "auth", "login"),
                ("gh", "auth", "status"),
                ("gh", "config", "get"),
                ("gh", "config", "set"),
                ("gh", "config", "get"),
            ],
        )
        self.assertFalse(runner.commands[1].mutate)
        self.assertFalse(runner.commands[2].mutate)
        self.assertFalse(runner.commands[4].mutate)
        self.assertFalse(any(command.argv[:2] == ("gh", "ssh-key") for command in runner.commands))

    def test_authenticated_session_skips_login_and_does_not_require_tty(self) -> None:
        runner = FakeRunner()
        with patch("ai_setup.config.github.interactive_terminal_available", return_value=False):
            GitHubConfigurator(runner).authenticate(  # type: ignore[arg-type]
                authenticated=True, protocol="ssh"
            )
        self.assertFalse(any(command.interactive for command in runner.commands))
        self.assertEqual(runner.commands, [])

    def test_no_tty_fails_before_login(self) -> None:
        runner = FakeRunner()
        with (
            patch("ai_setup.config.github.interactive_terminal_available", return_value=False),
            self.assertRaisesRegex(ValidationError, "run ai github from a terminal"),
        ):
            GitHubConfigurator(runner).authenticate(authenticated=False)  # type: ignore[arg-type]
        self.assertEqual(runner.commands, [])

    def test_zero_exit_without_authentication_does_not_configure_protocol(self) -> None:
        runner = FakeRunner(
            {("gh", "auth", "status", "--hostname", "github.com"): CommandResult((), 1, "", "")}
        )
        with (
            patch("ai_setup.config.github.interactive_terminal_available", return_value=True),
            self.assertRaisesRegex(ValidationError, "cancelled or did not complete"),
        ):
            GitHubConfigurator(runner).authenticate(authenticated=False)  # type: ignore[arg-type]
        self.assertFalse(
            any(command.argv[:3] == ("gh", "config", "set") for command in runner.commands)
        )


class CodexAuthenticationTests(unittest.TestCase):
    def manager(self, raw: str, runner: FakeRunner) -> CodexManager:
        home = Path(raw)
        manager = CodexManager(runner, home)  # type: ignore[arg-type]
        manager.shared_bin.parent.mkdir(parents=True)
        manager.shared_bin.write_text("binary", encoding="utf-8")
        manager.create_profiles()
        return manager

    def test_capability_check_prefers_device_auth_without_reading_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = FakeRunner()
            manager = self.manager(raw, runner)
            help_argv = (str(manager.bin_dir / "codex-01"), "login", "--help")
            runner.responses[help_argv] = CommandResult(help_argv, 0, "--device-auth\n", "")
            self.assertTrue(manager.device_auth_supported("01"))
            with patch("ai_setup.config.codex.interactive_terminal_available", return_value=True):
                manager.authenticate("01", device_auth=True)
            login = next(command for command in runner.commands if command.interactive)
            self.assertEqual(
                login.argv,
                (str(manager.bin_dir / "codex-01"), "login", "--device-auth"),
            )
            self.assertEqual(login.env, manual_authentication_env())
            self.assertFalse(
                any(command.argv[-1:] == ("auth.json",) for command in runner.commands)
            )

    def test_unsupported_device_auth_uses_normal_interactive_login(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = FakeRunner()
            manager = self.manager(raw, runner)
            self.assertFalse(manager.device_auth_supported("02"))
            with patch("ai_setup.config.codex.interactive_terminal_available", return_value=True):
                manager.authenticate("02", device_auth=False)
            login = next(command for command in runner.commands if command.interactive)
            self.assertEqual(login.argv, (str(manager.bin_dir / "codex-02"), "login"))
            self.assertEqual(login.env, manual_authentication_env())

    def test_no_tty_fails_before_login(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runner = FakeRunner()
            manager = self.manager(raw, runner)
            before = len(runner.commands)
            with (
                patch("ai_setup.config.codex.interactive_terminal_available", return_value=False),
                self.assertRaisesRegex(ValidationError, "run ai codex from a terminal"),
            ):
                manager.authenticate("01")
            self.assertFalse(any(command.interactive for command in runner.commands[before:]))


class AuthenticationStageTests(unittest.TestCase):
    def test_git_github_ssh_and_codex_remain_separate_ordered_stages(self) -> None:
        stages = tuple(
            stage
            for stage in STAGE_ORDER
            if stage
            in {
                WorkflowStage.GIT,
                WorkflowStage.GITHUB,
                WorkflowStage.SSH,
                WorkflowStage.CODEX,
            }
        )
        self.assertEqual(
            stages,
            (
                WorkflowStage.GIT,
                WorkflowStage.GITHUB,
                WorkflowStage.SSH,
                WorkflowStage.CODEX,
            ),
        )
