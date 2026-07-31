from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.cli import main
from ai_setup.config.codex import CodexManager
from ai_setup.config.ssh_inventory import SSHInventory
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

    def test_complete_plan_with_one_missing_application(self) -> None:
        transcript = Transcript()
        missing = next(p for p in self.catalog.apps if p.identifier == "mullvad-browser-bin")
        workflow = self.workflow(self.complete_plan, transcript)
        workflow.progress = WorkflowProgress(workflow._selected_stages((missing,)))
        workflow._render_plan((missing,))
        self.assertEqual(
            transcript.text,
            "Plan\n"
            "Administrator access.\n"
            "System update.\n"
            "Applications.\n"
            "Flatpak.\n"
            "Git.\n"
            "GitHub.\n"
            "SSH.\n"
            "Codex.\n"
            "Shell PATH.\n"
            "Verification.\n\n"
            "Missing application: Mullvad Browser from the AUR.\n"
            "Fourteen of fifteen software requirements are already present.\n",
        )

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
        self.assertIn(
            "Twelve of fifteen software requirements are already present.", transcript.text
        )
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
            "Plan\nGit.\nGitHub.\nVerification.\n\n"
            "All software requirements are already present.\n",
        )
        self.assertNotIn("Administrator access.", transcript.text)

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
                transcript.output("Existing SSH keys were preserved.")

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
            "Plan\nAdministrator access.\nSystem update.\nApplications.\nFlatpak.\n"
            "Git.\nGitHub.\nSSH.\nCodex.\nShell PATH.\nVerification.\n\n"
            "Missing application: Mullvad Browser from the AUR.\n"
            "Fourteen of fifteen software requirements are already present.\n\n"
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
            "The GitHub connection was verified.\nExisting SSH keys were preserved.\n\n"
            "Codex\ncodex-01 is not signed in.\nStarting sign-in for codex-01...\n\n"
            "Setup paused during Codex configuration.\n"
            "Completed: Administrator access, system update, applications, Flatpak, Git, GitHub, and SSH.\n"
            "Remaining: Codex, shell PATH, and verification.\n"
            "Completed changes were preserved.\nRun ai again to continue.",
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
            "\nSetup paused during administrator access.\n"
            "Remaining: Administrator access, system update, applications, Flatpak, Git, GitHub, SSH, Codex, shell PATH, and verification.\n"
            "No setup stages were completed.\nRun ai again to continue.",
        )

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
            "All verification checks passed.\n\nSetup complete.\nWorkstation ready.",
        )
        self.assertEqual(transcript.lines[-1], "Workstation ready.")

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
            "Run ai --verbose for complete command output.",
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
        dedicated = Mock(
            private_name=private.name,
            fingerprint="SHA256:new",
            protected=True,
        )
        manager = Mock()
        manager.key = private
        manager.inventory.side_effect = [
            SSHInventory(None, (), ()),
            SSHInventory(None, (dedicated,), ()),
        ]
        manager.inventory_remote.return_value = ()
        manager.create.return_value = True
        manager.verify.return_value = True
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
        for forbidden in ("ai 1.0.1", "\nArch Linux\n", "Shell:", "Step 1", "[01/", "Password:"):
            self.assertNotIn(forbidden, rendered)
        version = subprocess.run(
            (sys.executable, "-m", "ai_setup", "--version"),
            env={"PYTHONPATH": "src"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(version.stdout, "ai 1.0.1\n")
