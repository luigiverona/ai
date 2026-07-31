from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_setup.config.codex import CodexManager
from ai_setup.config.codex_installer import (
    TRUSTED_CODEX_INSTALLER,
    CodexInstallerProvenance,
    verify_codex_installer,
)
from ai_setup.errors import CommandError, ValidationError
from ai_setup.execution.runner import Command, CommandResult
from tests.helpers import FakeRunner
from tools.audit_codex_installer import audit


class InstallingRunner(FakeRunner):
    INSTALLER = b"#!/bin/sh\nprintf controlled-fixture\\n"

    def __init__(self, shared: Path) -> None:
        super().__init__()
        self.shared = shared

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        result = super().run(command, check=check)
        if command.argv[0] == "curl":
            output = Path(command.argv[command.argv.index("-o") + 1])
            output.write_bytes(self.INSTALLER)
            return CommandResult(
                command.argv,
                0,
                f"https://releases.openai.com/codex/install.sh\ntext/x-sh\n{len(self.INSTALLER)}\n",
                "",
            )
        if command.argv[0] == "sh":
            isolated_home = Path(command.env["HOME"])
            shell = command.env["SHELL"]
            profile = isolated_home / (".bashrc" if shell.endswith("bash") else ".profile")
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_text("upstream PATH mutation\n", encoding="utf-8")
            self.shared.parent.mkdir(parents=True, exist_ok=True)
            self.shared.write_text("binary", encoding="utf-8")
            self.shared.chmod(0o700)
        return result


def fixture_provenance(content: bytes) -> CodexInstallerProvenance:
    return replace(TRUSTED_CODEX_INSTALLER, sha256=hashlib.sha256(content).hexdigest())


class CodexTests(unittest.TestCase):
    def test_two_launchers_share_executable_forward_arguments_and_isolate_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            one = (manager.bin_dir / "codex-01").read_text(encoding="utf-8")
            two = (manager.bin_dir / "codex-02").read_text(encoding="utf-8")
            self.assertIn(f'exec "{manager.shared_bin}" "$@"', one)
            self.assertIn(f'exec "{manager.shared_bin}" "$@"', two)
            self.assertIn(str(manager.state_root / "01"), one)
            self.assertIn(str(manager.state_root / "02"), two)
            self.assertTrue(manager.profiles_distinct())

    def test_profile_permissions_and_existing_configuration_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            profile = manager.state_root / "01"
            profile.mkdir(parents=True)
            (profile / "config.toml").write_text('model = "example"\n', encoding="utf-8")
            manager.create_profiles()
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            self.assertEqual((profile / "config.toml").stat().st_mode & 0o777, 0o600)
            self.assertIn('model = "example"', (profile / "config.toml").read_text())
            self.assertEqual((manager.bin_dir / "codex-01").stat().st_mode & 0o777, 0o700)

    def test_official_installer_is_constrained_to_private_state_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            (workspace / "downloads").mkdir(parents=True)
            runner = InstallingRunner(home / "placeholder")
            manager = CodexManager(
                runner,
                home,
                workspace,
                installer_provenance=fixture_provenance(runner.INSTALLER),
            )
            runner.shared = manager.shared_bin
            startup_files = [
                home / ".bashrc",
                home / ".bash_profile",
                home / ".zshrc",
                home / ".zprofile",
                home / ".config/fish/conf.d/ai.fish",
            ]
            for path in startup_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"preserve {path.name}\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in startup_files}
            manager.install()
            installer_command = next(
                command for command in runner.commands if command.argv[0] == "sh"
            )
            installer_state = manager.state_root / "installer"
            self.assertEqual(installer_command.env["CODEX_HOME"], str(installer_state))
            self.assertEqual(
                installer_command.env["CODEX_INSTALL_DIR"], str(manager.shared_bin.parent)
            )
            self.assertEqual(installer_command.env["CODEX_RELEASE"], "latest")
            self.assertEqual(
                installer_command.env["HOME"], str(installer_state / "environment-home")
            )
            self.assertEqual(installer_command.env["SHELL"], "/bin/sh")
            self.assertTrue(installer_command.replace_env)
            self.assertNotIn("AI_WORKSTATION_POISON", installer_command.env)
            self.assertEqual(installer_state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((installer_state / "environment-home").stat().st_mode & 0o777, 0o700)
            self.assertEqual(before, {path: path.read_bytes() for path in startup_files})
            self.assertTrue((installer_state / "environment-home/.profile").is_file())
            self.assertFalse((home / ".codex").exists())
            self.assertFalse((home / ".local/bin/codex").exists())

    def test_fish_bash_and_zsh_files_are_untouched_by_installer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            (workspace / "downloads").mkdir(parents=True)
            shared = home / ".local/share/ai/codex/installer/bin/codex"
            runner = InstallingRunner(shared)
            manager = CodexManager(
                runner,
                home,
                workspace,
                installer_provenance=fixture_provenance(runner.INSTALLER),
            )
            runner.shared = manager.shared_bin
            watched = (
                home / ".bashrc",
                home / ".bash_profile",
                home / ".zshrc",
                home / ".zprofile",
                home / ".config/fish/config.fish",
            )
            with patch.dict(
                "os.environ", {"SHELL": "/usr/bin/fish", "AI_WORKSTATION_POISON": "yes"}
            ):
                manager.install()
            self.assertFalse(any(path.exists() for path in watched))

    def test_valid_managed_binary_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            self.assertTrue(manager.executable_valid())

    @patch("ai_setup.config.codex.shutil.which", return_value="/usr/bin/codex")
    def test_unrelated_system_codex_is_detected_without_changes(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = CodexManager(FakeRunner(), Path(raw))  # type: ignore[arg-type]
            self.assertEqual(manager.unrelated_codex(), Path("/usr/bin/codex"))
            self.assertFalse(manager.shared_bin.exists())

    def test_failed_artifact_verification_aborts_install(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            (workspace / "downloads").mkdir(parents=True)
            runner = InstallingRunner(home / "never-created")
            manager = CodexManager(runner, home, workspace)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValidationError, "differs from the audited version"):
                manager.install()
            self.assertFalse(manager.shared_bin.exists())

    def test_exact_bytes_and_transport_policy_fail_closed(self) -> None:
        trusted = b"#!/bin/sh\nprintf fixture\\n"
        provenance = replace(
            TRUSTED_CODEX_INSTALLER,
            sha256=hashlib.sha256(trusted).hexdigest(),
            maximum_bytes=len(trusted),
        )
        self.assertEqual(
            verify_codex_installer(
                trusted,
                effective_url="https://releases.openai.com/codex/install.sh",
                content_type="text/x-sh",
                reported_size=len(trusted),
                provenance=provenance,
            ),
            provenance.sha256,
        )
        variants = (
            trusted[:-1],
            trusted + b"x",
            trusted.replace(b"\n", b"\r\n"),
            b"X" + trusted[1:],
        )
        for content in variants:
            with self.subTest(content=content), self.assertRaises(ValidationError):
                verify_codex_installer(
                    content,
                    effective_url="https://releases.openai.com/codex/install.sh",
                    content_type="text/x-sh",
                    reported_size=len(content),
                    provenance=provenance,
                )
        for url in (
            "http://releases.openai.com/codex/install.sh",
            "https://example.com/install.sh",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                verify_codex_installer(
                    trusted,
                    effective_url=url,
                    content_type="text/x-sh",
                    reported_size=len(trusted),
                    provenance=provenance,
                )

    def test_oversized_installer_is_rejected(self) -> None:
        trusted = b"#!/bin/sh\n"
        provenance = replace(
            TRUSTED_CODEX_INSTALLER, sha256=hashlib.sha256(trusted).hexdigest(), maximum_bytes=4
        )
        with self.assertRaisesRegex(ValidationError, "size"):
            verify_codex_installer(
                trusted,
                effective_url="https://releases.openai.com/codex/install.sh",
                content_type="text/x-sh",
                reported_size=len(trusted),
                provenance=provenance,
            )

    def test_maintainer_audit_compares_source_without_execution(self) -> None:
        served = b"#!/bin/sh\nprintf fixture\\n"
        provenance = replace(TRUSTED_CODEX_INSTALLER, sha256=hashlib.sha256(served).hexdigest())
        with patch("tools.audit_codex_installer.TRUSTED_CODEX_INSTALLER", provenance):
            self.assertEqual(
                audit(
                    served,
                    served,
                    effective_url="https://releases.openai.com/codex/install.sh",
                    content_type="text/x-sh",
                ),
                provenance.sha256,
            )
            with self.assertRaisesRegex(ValueError, "differs"):
                audit(
                    served,
                    served + b"x",
                    effective_url="https://releases.openai.com/codex/install.sh",
                    content_type="text/x-sh",
                )

    def test_profile_specific_login_and_status_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            runner = FakeRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            with patch("ai_setup.config.codex.interactive_terminal_available", return_value=True):
                manager.authenticate("01")
                manager.authenticate("02")
            self.assertTrue(manager.verified("01"))
            self.assertTrue(manager.verified("02"))
            argv = [command.argv for command in runner.commands]
            self.assertIn((str(manager.bin_dir / "codex-01"), "login", "--device-auth"), argv)
            self.assertIn((str(manager.bin_dir / "codex-02"), "login", "--device-auth"), argv)
            self.assertTrue(
                all(
                    command.interactive
                    for command in runner.commands
                    if command.argv[-2:] == ("login", "--device-auth")
                )
            )
            self.assertIn((str(manager.bin_dir / "codex-01"), "login", "status"), argv)
            self.assertIn((str(manager.bin_dir / "codex-02"), "login", "status"), argv)

    def test_cancelled_profile_login_is_reported_cleanly(self) -> None:
        class CancellingRunner(FakeRunner):
            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                if "login" in command.argv and command.interactive:
                    raise CommandError("codex", "authenticate", "exit status 130", 130)
                return super().run(command, check=check)

        with tempfile.TemporaryDirectory() as raw:
            manager = CodexManager(CancellingRunner(), Path(raw))  # type: ignore[arg-type]
            with (
                patch("ai_setup.config.codex.interactive_terminal_available", return_value=True),
                self.assertRaisesRegex(ValidationError, "cancelled or did not complete"),
            ):
                manager.authenticate("02")

    def test_insecure_credential_permissions_fail_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            auth = manager.state_root / "01/auth.json"
            auth.write_text("not-a-real-credential", encoding="utf-8")
            auth.chmod(0o644)
            self.assertFalse(manager.verified("01"))

    def test_symbolic_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.state_root.mkdir(parents=True)
            unrelated = home / "unrelated"
            unrelated.mkdir()
            (manager.state_root / "01").symlink_to(unrelated)
            with self.assertRaises(ValidationError):
                manager.create_profiles()

    def test_launcher_updates_leave_no_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.create_profiles()
            manager.create_profiles()
            leftovers = list(manager.bin_dir.glob(".codex-*.new*"))
            self.assertEqual(leftovers, [])

    def test_unrelated_profile_launcher_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            launcher = home / ".local/bin/codex-01"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\nprintf unrelated\n")
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValidationError, "not recognized as managed"):
                manager.create_profiles()
            self.assertEqual(launcher.read_text(), "#!/bin/sh\nprintf unrelated\n")
