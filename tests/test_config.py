from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from ai_setup.config.codex import CodexManager
from ai_setup.config.git import GitConfigurator, GitIdentity
from ai_setup.config.ssh import SSHManager
from ai_setup.config.ssh_inventory import (
    RemoteKey,
    eligible_for_deletion,
    github_correlated_local_keys,
    inventory_local,
)
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import CommandRunner
from ai_setup.ui.terminal import Terminal
from tests.helpers import FakeRunner


class ConfigTests(unittest.TestCase):
    PUBLIC_KEY = (
        "ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
        "fixture@example\n"
    )

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

    def test_ssh_inventory_and_protected_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ssh = Path(raw) / ".ssh"
            ssh.mkdir()
            private = ssh / "id_ed25519_old"
            public = ssh / "id_ed25519_old.pub"
            private.write_text("private", encoding="utf-8")
            public.write_text(self.PUBLIC_KEY, encoding="utf-8")
            inventory = inventory_local(ssh)
            self.assertEqual(len(inventory.keys), 1)
            self.assertTrue(
                eligible_for_deletion(inventory.keys[0], ssh, ssh / "id_ed25519_ai_github")
            )

    def test_ssh_delete_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = SSHManager(FakeRunner(), Path(raw))  # type: ignore[arg-type]
            with self.assertRaises(PermissionError):
                manager.delete((), explicit_confirmation=False)

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

    def test_remote_deletion_requires_matching_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = SSHManager(FakeRunner(), Path(raw))  # type: ignore[arg-type]
            with self.assertRaises(PermissionError):
                manager.delete_remote(
                    (RemoteKey(1, "unrelated", "SHA256:no"),),
                    eligible_fingerprints=frozenset({"SHA256:yes"}),
                    explicit_confirmation=True,
                )

    def test_remote_key_material_is_suppressed_from_verbose_output(self) -> None:
        output: list[str] = []
        runner = CommandRunner(verbose=True, output=output.append)
        response = f"1\tfixture\t{self.PUBLIC_KEY.strip()}\n"
        with patch(
            "ai_setup.execution.runner.subprocess.run",
            return_value=CompletedProcess((), 0, response, ""),
        ):
            keys = SSHManager(runner, Path("/tmp/unused")).inventory_remote()
        self.assertEqual(keys[0].fingerprint, "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA")
        self.assertFalse(any("AAAAC3" in line for line in output))

    def test_only_github_correlated_local_keys_are_cleanup_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ssh = Path(raw) / ".ssh"
            ssh.mkdir()
            for name in ("github", "server"):
                (ssh / name).write_text("private fixture")
                (ssh / f"{name}.pub").write_text(self.PUBLIC_KEY)
            local = inventory_local(ssh).keys
            remote = (RemoteKey(1, "github", local[0].fingerprint),)
            self.assertEqual(github_correlated_local_keys(local, remote), local)

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

    def test_yes_never_approves_destructive_prompt(self) -> None:
        terminal = Terminal(input_fn=lambda _: "", output=lambda _: None)
        self.assertFalse(terminal.confirm("Delete?", assume_yes=True, destructive=True))

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
