from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.config.shell import ShellInfo
from ai_setup.config.ssh import SSHManager
from ai_setup.execution.runner import CommandRunner
from ai_setup.models import RunOptions
from ai_setup.status import ReadOnlyRunner, StatusWorkflow
from ai_setup.ui.terminal import Terminal
from ai_setup.verification import readiness

PATH_LINE = (
    'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'
)


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    result: dict[str, tuple[bytes, int, int]] = {}
    for path in sorted((root, *root.rglob("*"))):
        relative = str(path.relative_to(root)) if path != root else "."
        mode = stat.S_IMODE(path.lstat().st_mode)
        data = path.read_bytes() if path.is_file() else b""
        result[relative] = (data, mode, path.lstat().st_mtime_ns)
    return result


class RealStatusIntegrationTests(unittest.TestCase):
    @staticmethod
    def shell() -> ShellInfo:
        return ShellInfo("bash", Path("/bin/bash"), "test")

    def run_status(self, home: Path, path: Path) -> tuple[int, list[str], ReadOnlyRunner]:
        lines: list[str] = []
        runner = ReadOnlyRunner(output=lines.append)
        terminal = Terminal(
            input_fn=Mock(side_effect=AssertionError("status prompted")), output=lines.append
        )
        system_release = home.parent / "os-release"
        system_release.write_text("ID=arch\n", encoding="utf-8")
        with patch.dict(os.environ, {"PATH": str(path), "HOME": str(home)}, clear=False):
            code = StatusWorkflow(
                load_catalog(),
                RunOptions(home=home),
                terminal,
                runner=runner,
                target_shell=self.shell(),
                system_release=system_release,
            ).run()
        return code, lines, runner

    def test_incomplete_status_continues_after_missing_executables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            bin_dir = root / "bin"
            home.mkdir()
            bin_dir.mkdir()
            executable(bin_dir / "pacman", "exit 1\n")

            code, lines, runner = self.run_status(home, bin_dir)

            self.assertEqual(code, 1)
            output = "\n".join(lines)
            self.assertIn("Status", lines)
            self.assertIn("Git identity: Git is not installed.", lines)
            self.assertIn("GitHub authentication: GitHub CLI is not installed.", lines)
            self.assertIn("GitHub SSH protocol: GitHub CLI is not installed.", lines)
            self.assertIn("SSH connection: OpenSSH client is not installed.", lines)
            self.assertIn("codex-01: profile not authenticated.", lines)
            self.assertIn("codex-02: profile not authenticated.", lines)
            self.assertIn("Git: not installed.", lines)
            self.assertIn("Sober: not installed.", lines)
            self.assertIn("Flathub remote: Flatpak is not installed.", lines)
            self.assertEqual(lines[-1], "Workstation is not ready.")
            self.assertNotIn("Traceback", output)
            self.assertFalse(any(command.mutate for command in runner.history))
            self.assertFalse(any(command.argv[0] == "sudo" for command in runner.history))
            self.assertEqual(tuple(home.iterdir()), ())

    def test_ready_status_uses_real_orchestration_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            bin_dir = root / "bin"
            home.mkdir()
            bin_dir.mkdir()
            executable(bin_dir / "pacman", "exit 0\n")
            executable(bin_dir / "yay", "exit 0\n")
            executable(
                bin_dir / "flatpak",
                'if [ "$1" = remotes ]; then echo flathub; fi\nexit 0\n',
            )
            flatpak_repository = home / ".local/share/flatpak/repo"
            flatpak_repository.mkdir(parents=True)
            (flatpak_repository / "config").write_text("[core]\nrepo_version=1\n", encoding="utf-8")
            executable(
                bin_dir / "git",
                'case "$*" in *user.name) echo "Test User";; '
                "*user.email) echo test@example.com;; *init.defaultBranch) echo main;; esac\n",
            )
            executable(
                bin_dir / "gh",
                'case "$*" in *"config get git_protocol"*) echo ssh;; esac\nexit 0\n',
            )
            executable(
                bin_dir / "ssh",
                'echo "Hi test! You have successfully authenticated" >&2\nexit 1\n',
            )
            managed = home / ".local/share/ai/bin/codex"
            managed.parent.mkdir(parents=True)
            executable(managed, "exit 0\n")
            launcher_dir = home / ".local/bin"
            launcher_dir.mkdir(parents=True)
            for number in ("01", "02"):
                profile = home / f".local/share/ai/codex/{number}"
                profile.mkdir(parents=True, mode=0o700)
                launcher = launcher_dir / f"codex-{number}"
                executable(launcher, f'export CODEX_HOME="{profile}"\nexit 0\n')
                launcher.chmod(0o700)
            (home / ".bashrc").write_text(PATH_LINE + "\n", encoding="utf-8")
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir(mode=0o700)
            (ssh_dir / "known_hosts").write_text("github.com key\n", encoding="utf-8")
            (ssh_dir / "id_ed25519_ai_github").write_text("fixture\n", encoding="utf-8")
            before = snapshot(home)

            code, lines, runner = self.run_status(home, bin_dir)

            self.assertEqual(code, 0)
            self.assertEqual(lines[-1], "Workstation ready.")
            self.assertIn("All software requirements are ready.", lines)
            self.assertFalse(any(command.mutate for command in runner.history))
            self.assertEqual(snapshot(home), before)

    def test_status_safe_ssh_argv_and_filesystem_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            ssh_dir = home / ".ssh"
            owned = ssh_dir / "config.d/ai-github.conf"
            owned.parent.mkdir(parents=True)
            (ssh_dir / "known_hosts").write_text("github.com fixture\n", encoding="utf-8")
            (ssh_dir / "config").write_text("Include config.d/*.conf\n", encoding="utf-8")
            owned.write_text("Host github.com\n", encoding="utf-8")
            for path, mode in (
                (ssh_dir, 0o700),
                (ssh_dir / "config", 0o600),
                (ssh_dir / "known_hosts", 0o600),
                (owned, 0o600),
            ):
                path.chmod(mode)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            argv_log = root / "argv"
            executable(
                bin_dir / "ssh",
                'printf "%s\\n" "$@" >"$SSH_ARGV_LOG"\n'
                'echo "successfully authenticated" >&2\nexit 1\n',
            )
            before = snapshot(ssh_dir)
            runner = CommandRunner()
            with patch.dict(
                os.environ,
                {"PATH": str(bin_dir), "HOME": str(home), "SSH_ARGV_LOG": str(argv_log)},
                clear=False,
            ):
                self.assertTrue(SSHManager(runner, home).verify(read_only=True))
            self.assertEqual(snapshot(ssh_dir), before)
            argv = argv_log.read_text(encoding="utf-8").splitlines()
            expected = {
                "BatchMode=yes",
                "StrictHostKeyChecking=yes",
                "UpdateHostKeys=no",
                "ControlMaster=no",
                "ControlPersist=no",
                "ControlPath=none",
                "PermitLocalCommand=no",
                "IdentitiesOnly=yes",
                f"UserKnownHostsFile={ssh_dir / 'known_hosts'}",
            }
            self.assertTrue(expected.issubset(set(argv)))
            self.assertIn("/dev/null", argv)
            self.assertIn(str(ssh_dir / "id_ed25519_ai_github"), argv)


class StatusInterruptionTests(unittest.TestCase):
    def test_status_interruption_has_status_specific_message(self) -> None:
        lines: list[str] = []
        terminal = Terminal(output=lines.append)
        with patch.object(readiness.ReadinessVerifier, "results", side_effect=KeyboardInterrupt):
            code = StatusWorkflow(
                load_catalog(), RunOptions(home=Path("/tmp/status")), terminal
            ).run()
        self.assertEqual(code, 130)
        self.assertEqual(
            lines[-2:],
            ["Status check paused.", "Run ai status again to continue."],
        )


if __name__ == "__main__":
    unittest.main()
