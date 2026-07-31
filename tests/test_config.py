from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_setup.config.codex import CodexManager
from ai_setup.config.git import GitConfigurator, GitIdentity
from ai_setup.config.ssh import SSHManager
from ai_setup.errors import ApplicationError, ValidationError
from ai_setup.execution.runner import CommandRunner
from tests.helpers import FakeRunner


class ConfigTests(unittest.TestCase):
    def test_git_preserves_unrelated_configuration(self) -> None:
        runner = FakeRunner()
        GitConfigurator(runner).configure(GitIdentity("A", "a@example.com"))  # type: ignore[arg-type]
        self.assertTrue(
            all(
                command.argv[3] in {"user.name", "user.email", "init.defaultBranch"}
                for command in runner.commands
                if command.mutate
            )
        )

    def test_ssh_host_config_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "config").write_text("Host example.com\n    User me\n", encoding="utf-8")
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager._configure_host()
            config = (ssh / "config").read_text(encoding="utf-8")
            self.assertIn("Host example.com", config)
            self.assertEqual(config.count("Include ~/.ssh/config.d/ai-github.conf"), 1)
            self.assertIn("Host github.com", (ssh / "config.d/ai-github.conf").read_text())

    def test_unrelated_ssh_fragment_is_refused_without_changing_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ssh = home / ".ssh"
            fragment = ssh / "config.d/ai-github.conf"
            fragment.parent.mkdir(parents=True)
            config = ssh / "config"
            config.write_text("Host example.com\n")
            fragment.write_text("unrelated\n")
            with self.assertRaisesRegex(ValidationError, "not recognized as managed"):
                SSHManager(FakeRunner(), home)._configure_host()  # type: ignore[arg-type]
            self.assertEqual(config.read_text(), "Host example.com\n")
            self.assertEqual(fragment.read_text(), "unrelated\n")

    def test_dedicated_key_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            target = home / "unrelated"
            target.write_text("keep", encoding="utf-8")
            (ssh / "id_ed25519_ai_github").symlink_to(target)
            manager = SSHManager(FakeRunner(), home)  # type: ignore[arg-type]
            with self.assertRaises(ValidationError):
                manager.create("example@example.com")
            self.assertEqual(target.read_text(), "keep")

    def test_absent_dedicated_key_is_created_without_touching_unrelated_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ssh = home / ".ssh"
            ssh.mkdir()
            unrelated = ssh / "id_ed25519_unrelated"
            unrelated.write_bytes(b"unrelated private bytes")
            before = unrelated.read_bytes()
            manager = SSHManager(CommandRunner(output=lambda _: None), home)
            self.assertTrue(manager.create("fixture@example.com"))
            self.assertTrue(manager.key.is_file())
            self.assertTrue(manager.key.with_suffix(".pub").is_file())
            self.assertEqual(unrelated.read_bytes(), before)

    def test_recognized_dedicated_key_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = SSHManager(CommandRunner(output=lambda _: None), home)
            self.assertTrue(manager.create("fixture@example.com"))
            private = manager.key.read_bytes()
            public = manager.key.with_suffix(".pub").read_bytes()
            self.assertFalse(manager.create("changed@example.com"))
            self.assertEqual(manager.key.read_bytes(), private)
            self.assertEqual(manager.key.with_suffix(".pub").read_bytes(), public)

    def test_unrecognized_dedicated_collisions_are_refused_unchanged(self) -> None:
        for collision in ("private", "public"):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as raw:
                home = Path(raw)
                manager = SSHManager(CommandRunner(output=lambda _: None), home)
                self.assertTrue(manager.create("fixture@example.com"))
                private = manager.key
                public = manager.key.with_suffix(".pub")
                if collision == "private":
                    private.write_bytes(b"unrecognized private bytes")
                else:
                    public.write_text("ssh-ed25519 invalid fixture\n", encoding="ascii")
                before = (private.read_bytes(), public.read_bytes())
                with self.assertRaises(ApplicationError):
                    manager.create("fixture@example.com")
                self.assertEqual((private.read_bytes(), public.read_bytes()), before)

    def test_codex_launchers_have_separate_homes_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.create_profiles()
            one = (home / ".local/bin/codex-01").read_text()
            two = (home / ".local/bin/codex-02").read_text()
            self.assertIn("/01", one)
            self.assertIn("/02", two)
            self.assertNotEqual(one, two)
            self.assertEqual((home / ".local/share/ai/codex/01").stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (home / ".local/share/ai/codex/01/config.toml").stat().st_mode & 0o777, 0o600
            )
            self.assertFalse((home / ".local/bin/codex").exists())
