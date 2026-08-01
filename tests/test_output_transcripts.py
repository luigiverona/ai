from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.cli import main
from ai_setup.cli import parser as cli_parser
from ai_setup.config.codex import CodexManager
from ai_setup.errors import CommandError, ValidationError
from ai_setup.execution.runner import Command, CommandResult
from ai_setup.models import (
    Capability,
    Plan,
    RunOptions,
    Selection,
    WorkflowProgress,
    WorkflowStage,
)
from ai_setup.planning.planner import build_plan
from ai_setup.ui.terminal import Terminal
from ai_setup.workflow import Workflow
from tests.helpers import FakeRunner, controlled_executable_lookup


class Transcript:
    def __init__(self, answers: tuple[str, ...] = ()) -> None:
        self.lines: list[str] = []
        self.answers = iter(answers)

    def output(self, value: str) -> None:
        self.lines.append(value)

    def input(self, prompt: str) -> str:
        answer = next(self.answers, "")
        self.lines.append(prompt + answer)
        return answer

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class MostlyReadyRunner(FakeRunner):
    def __init__(self, transcript: Transcript, missing: str) -> None:
        super().__init__()
        self.transcript = transcript
        self.missing = missing
        self.updated = False

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        self.commands.append(command)
        argv = command.argv
        if argv == ("sudo", "-v"):
            self.transcript.output("[sudo] password for og:")
        if argv[:2] == ("sudo", "pacman") and "-Syu" in argv:
            self.updated = True
            return CommandResult(argv, 0, "upgraded one package\n", "")
        if argv[:2] == ("pacman", "-Q"):
            installed = argv[2] != self.missing or self.updated
            return CommandResult(argv, 0 if installed else 1, "", "")
        if argv[:3] == ("flatpak", "info", "--user"):
            return CommandResult(argv, 0, "", "")
        if argv[:3] == ("flatpak", "remotes", "--user"):
            return CommandResult(argv, 0, "flathub\n", "")
        if argv[:4] == ("git", "config", "--global", "--get"):
            values = {
                "user.name": "luigiverona\n",
                "user.email": "lluuigivveerona@gmail.com\n",
                "init.defaultBranch": "main\n",
            }
            return CommandResult(argv, 0, values.get(argv[4], ""), "")
        if argv[:3] == ("gh", "auth", "status"):
            return CommandResult(argv, 0, "", "")
        if argv[:3] == ("gh", "api", "user"):
            return CommandResult(argv, 0, "luigiverona\n", "")
        if argv[:3] == ("gh", "config", "get"):
            return CommandResult(argv, 0, "ssh\n", "")
        return CommandResult(argv, 0, "", "")


class GitIdentityRunner(FakeRunner):
    def __init__(self, name: str | None, email: str | None) -> None:
        super().__init__()
        self.values = {"user.name": name, "user.email": email, "init.defaultBranch": "main"}

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        self.commands.append(command)
        argv = command.argv
        if argv[:4] == ("git", "config", "--global", "--get"):
            value = self.values.get(argv[4])
            return CommandResult(argv, 0 if value else 1, f"{value}\n" if value else "", "")
        if argv[:3] == ("git", "config", "--global") and len(argv) == 5:
            self.values[argv[3]] = argv[4]
        return CommandResult(argv, 0, "", "")


class OutputTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            controlled_executable_lookup(
                {
                    "flatpak": "/usr/bin/flatpak",
                    "yay": "/usr/bin/yay",
                }
            )
        )
        self.catalog = load_catalog()
        self.complete_plan = build_plan(
            Selection(frozenset(Capability), complete=True), self.catalog
        )

    def workflow(
        self,
        plan: Plan,
        transcript: Transcript,
        *,
        runner: FakeRunner | None = None,
        dry_run: bool = False,
    ) -> Workflow:
        return Workflow(
            plan,
            RunOptions(dry_run=dry_run, home=Path("/tmp/ai-transcript-home")),
            Terminal(input_fn=transcript.input, output=transcript.output),
            runner=runner or FakeRunner(),  # type: ignore[arg-type]
        )

    def assert_major_section_spacing(self, lines: list[str], headings: tuple[str, ...]) -> None:
        self.assertTrue(lines)
        self.assertNotEqual(lines[0], "")
        self.assertNotEqual(lines[-1], "")
        for heading in headings[1:]:
            index = lines.index(heading)
            empty = 0
            while index - empty - 1 >= 0 and lines[index - empty - 1] == "":
                empty += 1
            self.assertEqual(empty, 1, f"spacing before {heading!r}: {lines!r}")

    def test_complete_plan_with_one_missing_application(self) -> None:
        transcript = Transcript()
        missing = next(p for p in self.catalog.apps if p.identifier == "mullvad-browser-bin")
        workflow = self.workflow(self.complete_plan, transcript)
        workflow.progress = WorkflowProgress(workflow._selected_stages((missing,)))
        workflow._render_plan((missing,))
        self.assertEqual(
            transcript.text,
            "Plan\n"
            "Request administrator access.\n"
            "Update Arch Linux.\n"
            "Install or verify applications.\n"
            "Configure or verify Flatpak and Flathub.\n"
            "Configure or verify Git.\n"
            "Configure or verify GitHub access.\n"
            "Configure or verify GitHub SSH access.\n"
            "Configure or verify both Codex profiles.\n"
            "Configure or verify the shell PATH.\n"
            "Verify the selected workstation state.\n\n"
            "Missing application: Mullvad Browser from the AUR.\n"
            "Fifteen of 16 software requirements are already present.\n",
        )

    def test_plan_to_first_stage_spacing_matches_interactive_and_yes(self) -> None:
        plan = build_plan(Selection(frozenset({Capability.CODEX}), complete=False), self.catalog)
        for assume_yes, answers in ((False, ("y",)), (True, ())):
            with self.subTest(assume_yes=assume_yes):
                transcript = Transcript(answers)
                workflow = Workflow(
                    plan,
                    RunOptions(assume_yes=assume_yes, home=Path("/tmp/ai-transcript-home")),
                    Terminal(input_fn=transcript.input, output=transcript.output),
                    runner=FakeRunner(),  # type: ignore[arg-type]
                )
                workflow.progress = WorkflowProgress(workflow._selected_stages(()))
                workflow._render_plan(())
                self.assertTrue(workflow.terminal.confirm("Continue?", assume_yes=assume_yes))
                workflow._begin(WorkflowStage.CODEX)
                workflow.terminal.output("Controlled stage output.")
                self.assert_major_section_spacing(transcript.lines, ("Plan", "Codex"))
                self.assertNotIn("\n\n\nCodex\n", transcript.text + "\n")

    def test_public_yes_collision_spacing_and_recovery_contract(self) -> None:
        plan = build_plan(Selection(frozenset({Capability.CODEX}), complete=False), self.catalog)
        transcript = Transcript()
        workflow = Workflow(
            plan,
            RunOptions(assume_yes=True, home=Path("/tmp/ai-transcript-home")),
            Terminal(input_fn=transcript.input, output=transcript.output),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow.progress = WorkflowProgress(workflow._selected_stages(()))
        workflow._render_plan(())
        self.assertTrue(workflow.terminal.confirm("Continue?", assume_yes=True))
        workflow._begin(WorkflowStage.CODEX)
        workflow._render_error(
            ValidationError(
                "managed file",
                "replace /tmp/disposable/.local/bin/codex-01",
                "the path exists but is not recognized as managed by ai; "
                "it was left unchanged; inspect the path and resolve the collision",
            )
        )
        self.assert_major_section_spacing(transcript.lines, ("Plan", "Codex"))
        self.assertIn("it was left unchanged", transcript.text)
        self.assertIn("Resolve the reported problem, then run ai codex.", transcript.text)
        self.assertNotIn("\n\n\nCodex\n", transcript.text + "\n")

    def test_complete_plan_with_multiple_or_no_missing_applications(self) -> None:
        missing = tuple(
            p
            for p in self.catalog.apps
            if p.identifier in {"mullvad-vpn", "mullvad-browser-bin", "org.vinegarhq.Sober"}
        )
        transcript = Transcript()
        workflow = self.workflow(self.complete_plan, transcript)
        workflow.progress = WorkflowProgress(workflow._selected_stages(missing))
        workflow._render_plan(missing)
        self.assertIn(
            "Missing applications:\n\nMullvad VPN from Arch Linux.\n"
            "Mullvad Browser from the AUR.\nSober from Flatpak.",
            transcript.text,
        )
        self.assertIn("Thirteen of 16 software requirements are already present.", transcript.text)
        ready = Transcript()
        ready_workflow = self.workflow(self.complete_plan, ready)
        ready_workflow.progress = WorkflowProgress(ready_workflow._selected_stages(()))
        ready_workflow._render_plan(())
        self.assertIn("All software requirements are already present.", ready.text)
        self.assertNotIn("Missing application", ready.text)

    def test_partial_git_github_plan(self) -> None:
        plan = build_plan(Selection(frozenset({Capability.GITHUB}), complete=False), self.catalog)
        transcript = Transcript()
        workflow = self.workflow(plan, transcript)
        workflow.progress = WorkflowProgress(workflow._selected_stages(()))
        workflow._render_plan(())
        self.assertEqual(
            transcript.text,
            "Plan\nConfigure or verify Git.\nConfigure or verify GitHub access.\n"
            "Verify the selected workstation state.\n\n"
            "All software requirements are already present.\n",
        )
        self.assertNotIn("administrator access", transcript.text)

    def test_reported_mostly_ready_interruption_transcript(self) -> None:
        transcript = Transcript(("y", "y"))
        missing = "mullvad-browser-bin"
        runner = MostlyReadyRunner(transcript, missing)
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            codex = home / ".local/share/ai/bin/codex"
            codex.parent.mkdir(parents=True)
            codex.write_text("managed", encoding="utf-8")
            flatpak_repository = home / ".local/share/flatpak/repo"
            flatpak_repository.mkdir(parents=True)
            (flatpak_repository / "config").write_text("[core]\nrepo_version=1\n", encoding="utf-8")
            workflow = Workflow(
                self.complete_plan,
                RunOptions(home=home),
                Terminal(input_fn=transcript.input, output=transcript.output),
                runner=runner,  # type: ignore[arg-type]
            )

            def ssh_ready() -> None:
                transcript.output("The dedicated key already exists.")
                transcript.output("The key is registered with GitHub.")
                transcript.output("The GitHub connection was verified.")

            def codex_interrupt(_: Path) -> None:
                transcript.output("codex-01 is not signed in.")
                transcript.output("Starting sign-in for codex-01...")
                raise KeyboardInterrupt

            with (
                patch("ai_setup.workflow.validate_system"),
                patch.object(workflow, "_ssh", side_effect=ssh_ready),
                patch.object(workflow, "_codex", side_effect=codex_interrupt),
            ):
                status = workflow.run()
        self.assertEqual(status, 130)
        self.assertEqual(
            transcript.text,
            "Plan\nRequest administrator access.\nUpdate Arch Linux.\n"
            "Install or verify applications.\nConfigure or verify Flatpak and Flathub.\n"
            "Configure or verify Git.\nConfigure or verify GitHub access.\n"
            "Configure or verify GitHub SSH access.\nConfigure or verify both Codex profiles.\n"
            "Configure or verify the shell PATH.\nVerify the selected workstation state.\n\n"
            "Missing application: Mullvad Browser from the AUR.\n"
            "Fifteen of 16 software requirements are already present.\n\n"
            "Continue? [y/N] y\n\n"
            "Administrator access\nSudo will ask for your password.\n[sudo] password for og:\n\n"
            "System update\nUpdating Arch Linux...\nSystem updated.\n\n"
            "Applications\nMullvad Browser was installed during the system update.\n"
            "No additional package installation was needed.\n\n"
            "Flatpak\nFlathub is already configured.\n"
            "All selected Flatpak applications are already installed.\n\n"
            "Git\nName: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
            "Keep this identity? [Y/n] y\nGit identity unchanged.\n\n"
            "GitHub\nAlready signed in as luigiverona.\nGit protocol already uses SSH.\n\n"
            "SSH\nThe dedicated key already exists.\nThe key is registered with GitHub.\n"
            "The GitHub connection was verified.\n\n"
            "Codex\ncodex-01 is not signed in.\nStarting sign-in for codex-01...\n\n"
            "Setup interrupted during Codex.\n"
            "Earlier completed stages remain valid: Administrator access, System update, "
            "Applications, Flatpak, Git, GitHub, and SSH.\n"
            "Later stages did not run: Shell PATH and Verification.\n"
            "Run ai codex to continue.",
        )

    def test_interruption_before_any_completed_stage(self) -> None:
        transcript = Transcript()
        workflow = self.workflow(self.complete_plan, transcript)
        workflow.progress = WorkflowProgress(
            tuple(WorkflowStage), current=WorkflowStage.ADMINISTRATOR
        )
        workflow._render_interruption()
        self.assertEqual(
            transcript.text,
            "\nSetup interrupted during Administrator access.\n"
            "No earlier stages completed.\n"
            "Later stages did not run: System update, Applications, Flatpak, Git, GitHub, "
            "SSH, Codex, Shell PATH, and Verification.\n"
            "Run ai setup to continue.",
        )

    def test_ctrl_c_before_confirmation_has_no_false_stage_or_success(self) -> None:
        transcript = Transcript()

        def interrupt(_: str) -> str:
            raise KeyboardInterrupt

        workflow = Workflow(
            self.complete_plan,
            RunOptions(dry_run=False, home=Path("/tmp/ai-transcript-home")),
            Terminal(input_fn=interrupt, output=transcript.output),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        with patch("ai_setup.workflow.validate_system"):
            status = workflow.run()
        self.assertEqual(status, 130)
        self.assertIn("Setup cancelled. No changes were made.", transcript.text)
        self.assertIn("Run ai setup to try again.", transcript.text)
        self.assertNotIn("Setup complete", transcript.text)

    def test_eof_at_confirmation_declines_without_mutation(self) -> None:
        transcript = Transcript()

        def ended(_: str) -> str:
            raise EOFError

        runner = FakeRunner()
        workflow = Workflow(
            self.complete_plan,
            RunOptions(home=Path("/tmp/ai-transcript-home")),
            Terminal(input_fn=ended, output=transcript.output),
            runner=runner,  # type: ignore[arg-type]
        )
        with patch("ai_setup.workflow.validate_system"):
            status = workflow.run()
        self.assertEqual(status, 0)
        self.assertIn("Input ended; confirmation declined.", transcript.text)
        self.assertTrue(transcript.text.endswith("No changes were made."))
        self.assertFalse(any(command.mutate for command in runner.commands))

    def test_successful_verification_and_completion_transcript(self) -> None:
        transcript = Transcript()
        workflow = self.workflow(Plan((Capability.GIT, Capability.GITHUB), (), ()), transcript)
        workflow.progress = WorkflowProgress(
            (WorkflowStage.GIT, WorkflowStage.GITHUB, WorkflowStage.VERIFICATION)
        )
        runner = workflow.runner
        runner.responses.update(  # type: ignore[attr-defined]
            {
                ("git", "config", "--global", "--get", "user.name"): CommandResult(
                    (), 0, "A\n", ""
                ),
                ("git", "config", "--global", "--get", "user.email"): CommandResult(
                    (), 0, "a@example.com\n", ""
                ),
                ("git", "config", "--global", "--get", "init.defaultBranch"): CommandResult(
                    (), 0, "main\n", ""
                ),
                ("gh", "auth", "status", "--hostname", "github.com"): CommandResult((), 0, "", ""),
                ("gh", "config", "get", "git_protocol", "--host", "github.com"): CommandResult(
                    (), 0, "ssh\n", ""
                ),
            }
        )
        with patch(
            "ai_setup.verification.readiness.Verifier.system",
            return_value=type("Check", (), {"passed": True})(),
        ):
            workflow._verify()
        workflow._render_completion()
        self.assertEqual(
            transcript.text,
            "Verification\nThe Git identity is ready.\nGitHub authentication is ready.\n"
            "All verification checks passed.\n\nCommand complete.\n"
            "Selected configuration is ready.",
        )
        self.assertEqual(transcript.lines[-1], "Selected configuration is ready.")

    def test_representative_package_failure_transcript(self) -> None:
        transcript = Transcript()
        package = next(p for p in self.catalog.apps if p.identifier == "mullvad-browser-bin")
        workflow = self.workflow(Plan((Capability.APPS,), (), (package,)), transcript)
        workflow.progress = WorkflowProgress(
            (WorkflowStage.APPLICATIONS, WorkflowStage.VERIFICATION)
        )
        workflow.progress.current = WorkflowStage.APPLICATIONS
        workflow._render_error(
            CommandError(
                "AUR installation",
                "install packages",
                "makepkg exited with status 1",
                1,
                "/tmp/ai-test/logs/aur.log",
                ("mullvad-browser-bin",),
            )
        )
        self.assertEqual(
            transcript.text,
            "Mullvad Browser could not be installed.\n"
            "Reason: makepkg exited with status 1.\n"
            "Details: /tmp/ai-test/logs/aur.log.\n"
            "\nSetup stopped at Applications.\n"
            "No earlier stages completed.\n"
            "Later stages did not run: Verification.\n"
            "Resolve the reported problem, then run ai apps.\n"
            "Run ai apps --verbose for diagnostic output.",
        )

    def test_new_git_identity_transcript(self) -> None:
        transcript = Transcript(("luigiverona", "lluuigivveerona@gmail.com", "y"))
        runner = GitIdentityRunner(None, None)
        workflow = self.workflow(Plan((Capability.GIT,), (), ()), transcript, runner=runner)
        workflow._git()
        self.assertEqual(
            transcript.text,
            "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
            "Use this identity? [Y/n] y\nGit identity saved.",
        )
        self.assertEqual(runner.values["user.name"], "luigiverona")
        self.assertEqual(runner.values["user.email"], "lluuigivveerona@gmail.com")

    def test_existing_git_identity_is_kept(self) -> None:
        transcript = Transcript(("y",))
        runner = GitIdentityRunner("luigiverona", "lluuigivveerona@gmail.com")
        workflow = self.workflow(Plan((Capability.GIT,), (), ()), transcript, runner=runner)
        workflow._git()
        self.assertEqual(
            transcript.text,
            "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
            "Keep this identity? [Y/n] y\nGit identity unchanged.",
        )

    def test_existing_git_identity_is_replaced(self) -> None:
        transcript = Transcript(("n", "Luigi Verona", "luigi@example.com", "y"))
        runner = GitIdentityRunner("luigiverona", "lluuigivveerona@gmail.com")
        workflow = self.workflow(Plan((Capability.GIT,), (), ()), transcript, runner=runner)
        workflow._git()
        self.assertEqual(
            transcript.text,
            "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
            "Keep this identity? [Y/n] n\nNew name: Luigi Verona\n"
            "New email: luigi@example.com\nUse this identity? [Y/n] y\n"
            "Git identity updated.",
        )
        self.assertEqual(runner.values["user.name"], "Luigi Verona")
        self.assertEqual(runner.values["user.email"], "luigi@example.com")

    def test_rejected_replacement_retains_existing_git_identity(self) -> None:
        transcript = Transcript(("n", "Luigi Verona", "luigi@example.com", "n"))
        runner = GitIdentityRunner("luigiverona", "lluuigivveerona@gmail.com")
        workflow = self.workflow(Plan((Capability.GIT,), (), ()), transcript, runner=runner)
        workflow._git()
        self.assertEqual(
            transcript.text,
            "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
            "Keep this identity? [Y/n] n\nNew name: Luigi Verona\n"
            "New email: luigi@example.com\nUse this identity? [Y/n] n\n"
            "Git identity unchanged.",
        )
        self.assertEqual(runner.values["user.name"], "luigiverona")
        self.assertEqual(runner.values["user.email"], "lluuigivveerona@gmail.com")

    def test_assume_yes_keeps_existing_git_identity_without_prompts(self) -> None:
        transcript = Transcript()
        runner = GitIdentityRunner("luigiverona", "lluuigivveerona@gmail.com")
        workflow = Workflow(
            Plan((Capability.GIT,), (), ()),
            RunOptions(assume_yes=True, home=Path("/tmp/test-home")),
            Terminal(input_fn=transcript.input, output=transcript.output),
            runner=runner,  # type: ignore[arg-type]
        )
        workflow._git()
        self.assertEqual(
            transcript.text,
            "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\nGit identity unchanged.",
        )

    def test_empty_git_replacement_values_are_rejected(self) -> None:
        for answers, reason, expected in (
            (
                ("n", ""),
                "name cannot be empty",
                "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
                "Keep this identity? [Y/n] n\nNew name: ",
            ),
            (
                ("n", "Luigi Verona", ""),
                "email cannot be empty",
                "Name: luigiverona\nEmail: lluuigivveerona@gmail.com\n"
                "Keep this identity? [Y/n] n\nNew name: Luigi Verona\nNew email: ",
            ),
        ):
            with self.subTest(reason=reason):
                transcript = Transcript(answers)
                runner = GitIdentityRunner("luigiverona", "lluuigivveerona@gmail.com")
                workflow = self.workflow(Plan((Capability.GIT,), (), ()), transcript, runner=runner)
                with self.assertRaisesRegex(ValidationError, reason):
                    workflow._git()
                self.assertEqual(transcript.text, expected)
                self.assertEqual(runner.values["user.name"], "luigiverona")
                self.assertEqual(runner.values["user.email"], "lluuigivveerona@gmail.com")

    def test_new_github_authentication_transcript(self) -> None:
        transcript = Transcript()

        class GitHubRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.authenticated = False
                self.protocol = "https"

            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                if command.argv[:3] == ("gh", "auth", "status"):
                    return CommandResult(command.argv, 0 if self.authenticated else 1, "", "")
                if command.argv[:3] == ("gh", "auth", "login"):
                    self.authenticated = True
                if command.argv[:3] == ("gh", "api", "user"):
                    return CommandResult(command.argv, 0, "luigiverona\n", "")
                if command.argv[:3] == ("gh", "config", "set"):
                    self.protocol = "ssh"
                if command.argv[:3] == ("gh", "config", "get"):
                    return CommandResult(command.argv, 0, self.protocol + "\n", "")
                return CommandResult(command.argv, 0, "", "")

        workflow = self.workflow(
            Plan((Capability.GITHUB,), (), ()), transcript, runner=GitHubRunner()
        )
        with patch("ai_setup.config.github.interactive_terminal_available", return_value=True):
            workflow._github()
        self.assertEqual(
            transcript.text,
            "Starting manual authentication.\n"
            "Open the URL shown below in your browser and enter the displayed code.\n"
            "Signed in as luigiverona.\n"
            "Git protocol changed to SSH.",
        )

    def test_new_ssh_key_transcript(self) -> None:
        transcript = Transcript()
        workflow = self.workflow(Plan((Capability.SSH,), (), ()), transcript)
        private = Path("/tmp/home/.ssh/id_ed25519_ai_github")
        manager = Mock()
        manager.key = private
        manager.create.return_value = True
        manager.verify.side_effect = [False, True]
        with (
            patch("ai_setup.workflow.SSHManager", return_value=manager),
            patch("ai_setup.workflow.GitHubConfigurator.account", return_value="luigiverona"),
            patch("ai_setup.workflow.GitConfigurator.get", return_value="a@example.com"),
        ):
            workflow._ssh()
        self.assertEqual(
            transcript.text,
            "Creating a dedicated SSH key...\nThe dedicated key was created.\n"
            "Registering the key with GitHub...\nThe key was registered with GitHub.\n"
            "The GitHub connection was verified.",
        )

    def test_existing_codex_profiles_transcript(self) -> None:
        transcript = Transcript()
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            workspace.mkdir()
            runner = FakeRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("managed", encoding="utf-8")
            manager.create_profiles()
            workflow = Workflow(
                Plan((Capability.CODEX,), (), ()),
                RunOptions(home=home),
                Terminal(input_fn=transcript.input, output=transcript.output),
                runner=runner,  # type: ignore[arg-type]
            )
            workflow._codex(workspace)
        self.assertEqual(
            transcript.text,
            "codex-01 is already signed in.\ncodex-02 is already signed in.\n"
            "Both Codex profiles are ready.",
        )

    def test_codex_manual_device_authentication_transcript(self) -> None:
        transcript = Transcript()
        manager = Mock()
        manager.unrelated_codex.return_value = None
        manager.executable_valid.return_value = True
        manager.verified.side_effect = [True, False]
        manager.device_auth_supported.return_value = True
        workflow = self.workflow(
            Plan((Capability.CODEX,), (), ()),
            transcript,
        )
        with patch("ai_setup.workflow.CodexManager", return_value=manager):
            workflow._codex(Path("/tmp/ai-workspace"))
        self.assertEqual(
            transcript.text,
            "codex-01 is already signed in.\n"
            "codex-02 is not signed in.\n"
            "Starting manual device authentication for codex-02.\n"
            "Open the URL shown below in your browser and enter the displayed code.\n"
            "codex-02 signed in.\n"
            "Both Codex profiles are ready.",
        )
        manager.authenticate.assert_called_once_with("02", device_auth=True)

    def test_codex_manual_fallback_transcript(self) -> None:
        transcript = Transcript()
        manager = Mock()
        manager.unrelated_codex.return_value = None
        manager.executable_valid.return_value = True
        manager.verified.side_effect = [False, True]
        manager.device_auth_supported.return_value = False
        workflow = self.workflow(
            Plan((Capability.CODEX,), (), ()),
            transcript,
        )
        with patch("ai_setup.workflow.CodexManager", return_value=manager):
            workflow._codex(Path("/tmp/ai-workspace"))
        self.assertIn(
            "Device authentication is unavailable for codex-01.\n"
            "Starting manual authentication for codex-01.\n"
            "Open the URL shown below in your browser.",
            transcript.text,
        )
        manager.authenticate.assert_called_once_with("01", device_auth=False)

    def test_stage_spacing_and_forbidden_decoration(self) -> None:
        transcript = Transcript()
        terminal = Terminal(output=transcript.output)
        terminal.section("Applications")
        terminal.output("All selected applications are already installed.")
        terminal.section("GitHub")
        terminal.output("Already signed in as luigiverona.")
        self.assertEqual(
            transcript.text,
            "Applications\nAll selected applications are already installed.\n\n"
            "GitHub\nAlready signed in as luigiverona.",
        )
        for forbidden in ("[01/", "Step 1", "1. Applications", "---", "✓", "█"):
            self.assertNotIn(forbidden, transcript.text)

    def test_help_is_plain_and_deterministic_at_representative_widths(self) -> None:
        catalog = load_catalog()
        root = cli_parser(catalog)
        baseline = root.format_help()
        choices = root._subparsers._group_actions[0].choices  # type: ignore[union-attr]
        for width in (60, 80, 120, None):
            with (
                self.subTest(width=width),
                patch(
                    "shutil.get_terminal_size",
                    side_effect=OSError if width is None else None,
                    return_value=None if width is None else os.terminal_size((width, 24)),
                ),
            ):
                self.assertEqual(cli_parser(catalog).format_help(), baseline)
        for command, command_parser in choices.items():
            with self.subTest(command=command):
                rendered = command_parser.format_help()
                self.assertNotRegex(rendered, r"\x1b\[[0-9;]*[A-Za-z]")
                self.assertNotIn("✓", rendered)
                self.assertNotIn("│", rendered)

    def test_normal_cli_has_no_banner_and_version_remains_available(self) -> None:
        output = io.StringIO()

        def render_plan(workflow: Workflow) -> int:
            workflow.terminal.output("Plan")
            workflow.terminal.output("Verification.")
            return 0

        with contextlib.redirect_stdout(output), patch.object(Workflow, "run", render_plan):
            status = main(["--dry-run"])
        self.assertEqual(status, 0)
        rendered = output.getvalue()
        self.assertTrue(rendered.startswith("Plan\n"))
        for forbidden in ("ai 2.1.0", "\nArch Linux\n", "Shell:", "Step 1", "[01/", "Password:"):
            self.assertNotIn(forbidden, rendered)
        version = subprocess.run(
            (sys.executable, "-m", "ai_setup", "--version"),
            env={"PYTHONPATH": "src"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(version.stdout, "ai 2.1.0\n")
