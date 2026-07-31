from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_setup.config.shell import (
    ProcessShell,
    ShellInfo,
    configure_path,
    current_path_contains,
    detect_shell,
    path_configured,
)
from ai_setup.errors import ValidationError
from ai_setup.verification.checks import Verifier
from tests.helpers import FakeRunner


class ShellTests(unittest.TestCase):
    def test_fish_detected_through_noninteractive_bash_bootstrap(self) -> None:
        shell = detect_shell(
            env={"SHELL": "/bin/bash"},
            login_shell="/usr/bin/fish",
            ancestry=(ProcessShell("bash", False), ProcessShell("fish", True)),
        )
        self.assertEqual((shell.name, shell.source), ("fish", "interactive process"))

    def test_actual_interactive_bash_wins(self) -> None:
        shell = detect_shell(
            env={"SHELL": "/usr/bin/zsh"},
            login_shell="/usr/bin/fish",
            ancestry=(ProcessShell("bash", True),),
        )
        self.assertEqual(shell.name, "bash")

    def test_actual_interactive_zsh_wins(self) -> None:
        shell = detect_shell(
            env={"SHELL": "/usr/bin/bash"},
            login_shell="/usr/bin/fish",
            ancestry=(ProcessShell("zsh", True),),
        )
        self.assertEqual(shell.name, "zsh")

    def test_misleading_shell_environment_does_not_override_login(self) -> None:
        shell = detect_shell(
            env={"SHELL": "/bin/bash"},
            login_shell="/usr/bin/fish",
            ancestry=(ProcessShell("bash", False),),
        )
        self.assertEqual((shell.name, shell.source), ("fish", "login account"))

    def test_root_environment_contamination_does_not_change_target_login(self) -> None:
        shell = detect_shell(
            uid=1000,
            env={"HOME": "/root", "SHELL": "/bin/bash", "SUDO_USER": "person"},
            login_shell="/usr/bin/fish",
            ancestry=(ProcessShell("bash", False),),
        )
        self.assertEqual(shell.name, "fish")

    def test_selected_shell_only_is_configured_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            fish = ShellInfo("fish", Path("/usr/bin/fish"), "test")
            update = configure_path(home, fish, env={"PATH": "/usr/bin"})
            self.assertEqual(update.path, home / ".config/fish/conf.d/ai.fish")
            self.assertTrue(Verifier(FakeRunner(), home).shell_configuration(fish).passed)  # type: ignore[arg-type]
            self.assertFalse((home / ".bashrc").exists())
            self.assertFalse((home / ".zshrc").exists())

    def test_bash_and_zsh_use_interactive_startup_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            bash = configure_path(
                home, ShellInfo("bash", Path("/usr/bin/bash"), "test"), env={"PATH": ""}
            )
            zsh = configure_path(
                home, ShellInfo("zsh", Path("/usr/bin/zsh"), "test"), env={"PATH": ""}
            )
            self.assertEqual(bash.path, home / ".bashrc")
            self.assertEqual(zsh.path, home / ".zshrc")

    def test_repeated_configuration_is_idempotent_and_preserves_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            shell = ShellInfo("bash", Path("/usr/bin/bash"), "test")
            startup = home / ".bashrc"
            startup.write_text("alias ll='ls -l'\n", encoding="utf-8")
            first = configure_path(home, shell, env={"PATH": "/usr/bin"})
            second = configure_path(home, shell, env={"PATH": "/usr/bin"})
            text = startup.read_text(encoding="utf-8")
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(text.count("$HOME/.local/bin:$PATH"), 1)
            self.assertIn("alias ll='ls -l'", text)

    def test_current_path_and_new_session_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            shell = ShellInfo("fish", Path("/usr/bin/fish"), "test")
            present = configure_path(home, shell, env={"PATH": f"{home}/.local/bin:/usr/bin"})
            self.assertTrue(present.current_session_has_path)
            self.assertFalse(present.new_session_required)
            other_home = home / "other"
            other_home.mkdir()
            missing = configure_path(other_home, shell, env={"PATH": "/usr/bin"})
            self.assertFalse(missing.current_session_has_path)
            self.assertTrue(missing.new_session_required)
            self.assertTrue(current_path_contains(home, {"PATH": f"{home}/.local/bin"}))

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            shell = ShellInfo("zsh", Path("/usr/bin/zsh"), "test")
            update = configure_path(home, shell, dry_run=True, env={"PATH": ""})
            self.assertTrue(update.changed)
            self.assertFalse(update.path.exists())
            self.assertFalse(path_configured(home, shell))

    def test_symbolic_startup_file_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            target = home / "real-bashrc"
            target.write_text("keep\n", encoding="utf-8")
            (home / ".bashrc").symlink_to(target)
            shell = ShellInfo("bash", Path("/usr/bin/bash"), "test")
            with self.assertRaises(ValidationError):
                configure_path(home, shell, env={"PATH": ""})
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_unrelated_dedicated_fish_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            fish = home / ".config/fish/conf.d/ai.fish"
            fish.parent.mkdir(parents=True)
            fish.write_text("user-owned fish content\n")
            with self.assertRaisesRegex(ValidationError, "not recognized as managed"):
                configure_path(
                    home,
                    ShellInfo("fish", Path("/usr/bin/fish"), "test"),
                    env={"PATH": ""},
                )
            self.assertEqual(fish.read_text(), "user-owned fish content\n")
