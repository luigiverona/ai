from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_setup.ui.terminal import Terminal
from ai_setup.verification.checks import Verifier
from tests.helpers import FakeRunner
from tools.build_installer import build_installer


class UiVerificationBootstrapTests(unittest.TestCase):
    def _require_arch_nonroot(self) -> None:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
        if "ID=arch" not in release:
            self.skipTest("bootstrap integration requires Arch Linux")
        if os.geteuid() == 0:
            self.skipTest("bootstrap intentionally rejects root")

    def test_terminal_has_plain_sections_and_defaults(self) -> None:
        output: list[str] = []
        terminal = Terminal(input_fn=lambda _: "", output=output.append)
        terminal.section("Git configuration")
        terminal.output("Content.")
        terminal.section("Next")
        self.assertEqual(output, ["Git configuration", "Content.", "", "Next"])
        self.assertTrue(terminal.confirm("Keep?", default=True))
        self.assertFalse(terminal.confirm("Delete?", default=False))

    def test_confirmation_retries_invalid_input_and_declines_eof(self) -> None:
        answers = iter(("maybe", "YES"))
        output: list[str] = []
        terminal = Terminal(input_fn=lambda _: next(answers), output=output.append)
        self.assertTrue(terminal.confirm("Continue?"))
        self.assertEqual(output, ["Please answer yes or no."])

        def ended(_: str) -> str:
            raise EOFError

        output.clear()
        terminal = Terminal(input_fn=ended, output=output.append)
        self.assertFalse(terminal.confirm("Continue?"))
        self.assertEqual(output, ["Input ended; confirmation declined."])

    def test_package_failure_is_compact_and_actionable(self) -> None:
        output: list[str] = []
        Terminal(output=output.append).error(
            "AUR installation",
            "install packages",
            "old and new are in conflict",
            "/tmp/ai-test/logs/aur.log",
            ("mullvad-browser-bin",),
        )
        self.assertEqual(
            output,
            [
                "AUR installation failed.",
                "Packages: mullvad-browser-bin.",
                "Reason: old and new are in conflict.",
                "Details: /tmp/ai-test/logs/aur.log.",
                "Run ai --verbose for complete command output.",
            ],
        )

    @patch("ai_setup.verification.checks.platform.machine", return_value="wrong")
    def test_verification_failure(self, _: object) -> None:
        result = Verifier(FakeRunner(), Path.home()).system()  # type: ignore[arg-type]
        self.assertFalse(result.passed)

    def test_bootstrap_syntax(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(("bash", "-n", str(root / "bootstrap/install.in")), check=False)
        self.assertEqual(result.returncode, 0)

    def test_bootstrap_does_not_execute_ai(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "bootstrap/install.in").read_text()
        self.assertIn("Run ai to set up the workstation", text)
        self.assertNotIn("exec ai", text)

    def _bootstrap_fixture(
        self, root: Path, *, unsafe_link: bool = False
    ) -> tuple[Path, Path, dict[str, str]]:
        archive = root / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for directory in (
                "ai-9.8.7",
                "ai-9.8.7/apps",
                "ai-9.8.7/deps",
                "ai-9.8.7/src",
                "ai-9.8.7/src/ai_setup",
            ):
                info = tarfile.TarInfo(directory)
                info.type = tarfile.DIRTYPE
                bundle.addfile(info)
            for name, content in (
                ("ai-9.8.7/pyproject.toml", b"[project]\nname='ai-workstation'\n"),
                ("ai-9.8.7/src/ai_setup/__init__.py", b""),
                (
                    "ai-9.8.7/src/ai_setup/__main__.py",
                    b"from ai_setup.cli import main\nraise SystemExit(main())\n",
                ),
                (
                    "ai-9.8.7/src/ai_setup/cli.py",
                    b"def main():\n print('ai 9.8.7')\n return 0\n",
                ),
            ):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
            if unsafe_link:
                link = tarfile.TarInfo("ai-9.8.7/escape")
                link.type = tarfile.SYMTYPE
                link.linkname = "/tmp/escape"
                bundle.addfile(link)
        named_archive = root / "ai-9.8.7.tar.gz"
        archive.rename(named_archive)
        archive = named_archive
        installer = root / "install"
        build_installer(
            Path(__file__).resolve().parents[1] / "bootstrap/install.in",
            "9.8.7",
            archive,
            installer,
        )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        scripts = {
            "getent": '#!/bin/sh\nprintf \'%s:x:1000:1000:test:%s:%s\\n\' "$2" "$HOME" "$FAKE_LOGIN_SHELL"\n',
            "ps": "#!/bin/sh\ncase \" $* \" in *' comm='*) echo \"$FAKE_PROCESS_SHELL\";; *' args='*) echo \"$FAKE_PROCESS_SHELL\";; *' tty='*) echo pts/1;; *' ppid='*) echo 0;; esac\n",
            "pacman": "#!/bin/sh\nexit 0\n",
            "curl": '#!/bin/sh\nout=\'\'\nurl=\'\'\nprevious=\'\'\nfor arg in "$@"; do if [ "$previous" = -o ]; then out=$arg; fi; previous=$arg; url=$arg; done\nprintf \'%s\\n\' "$url" >>"$CURL_LOG"\ncase "$url" in *.sha256) exit 97;; *) cp "$FIXTURE_ARCHIVE" "$out";; esac\n',
            "stat": '#!/bin/sh\nfor last do :; done\nif [ -n "${FAKE_WRONG_OWNER_PATH:-}" ] && [ "$last" = "$FAKE_WRONG_OWNER_PATH" ]; then printf "999\\n"; else exec /usr/bin/stat "$@"; fi\n',
        }
        for name, text in scripts.items():
            path = fake_bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(root / "home"),
                "USER": "bootstrap-user",
                "SHELL": "/bin/bash",
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FIXTURE_ARCHIVE": str(archive),
                "FAKE_LOGIN_SHELL": "/usr/bin/fish",
                "FAKE_PROCESS_SHELL": "fish",
                "CURL_LOG": str(root / "curl.log"),
            }
        )
        Path(env["HOME"]).mkdir()
        return archive, installer, env

    def _assert_fixture_cli(self, env: dict[str, str]) -> None:
        launcher = Path(env["HOME"]) / ".local/bin/ai"
        for argument in ("--version", "--help", "--dry-run"):
            result = subprocess.run(
                (str(launcher), argument), env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ai 9.8.7", result.stdout)

    def test_bootstrap_fish_detection_atomic_install_and_idempotent_path(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            first = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            second = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                first.stdout,
                "Installing ai.\n"
                "Downloading the release... done.\n"
                "Verifying the release... done.\n"
                "Installing the command... done.\n"
                "Configuring the fish PATH... done.\n\n"
                "The ai command is installed.\n"
                "Run ai to set up the workstation.\n",
            )
            self.assertEqual(
                second.stdout,
                "Installing ai.\n"
                "Downloading the release... done.\n"
                "Verifying the release... done.\n"
                "Installing the command... done.\n"
                "The fish PATH is already configured.\n\n"
                "The ai command is installed.\n"
                "Run ai to set up the workstation.\n",
            )
            self.assertNotIn("% Total", first.stdout + first.stderr)
            home = Path(env["HOME"])
            self.assertTrue((home / ".local/bin/ai").is_file())
            self.assertTrue((home / ".local/share/ai/current").is_symlink())
            self.assertTrue((home / ".local/share/ai/current/src/ai_setup/cli.py").is_file())
            fish = home / ".config/fish/conf.d/ai.fish"
            self.assertEqual(fish.read_text().count("fish_add_path"), 1)
            self.assertFalse((home / ".bashrc").exists())
            self.assertNotIn(".sha256", (root / "curl.log").read_text())
            self.assertNotRegex(installer.read_text(encoding="utf-8"), r"(?m)^\s*sudo\s")
            self._assert_fixture_cli(env)

    def test_bootstrap_bash_and_zsh_are_shell_specific(self) -> None:
        self._require_arch_nonroot()
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                env["FAKE_LOGIN_SHELL"] = f"/usr/bin/{shell}"
                env["FAKE_PROCESS_SHELL"] = shell
                result = subprocess.run(
                    ("bash", str(installer)), env=env, text=True, capture_output=True
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                repeated = subprocess.run(
                    ("bash", str(installer)), env=env, text=True, capture_output=True
                )
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                home = Path(env["HOME"])
                selected = home / f".{shell}rc"
                self.assertEqual(selected.read_text().count("Added by ai"), 1)
                other = home / (".zshrc" if shell == "bash" else ".bashrc")
                self.assertFalse(other.exists())
                self.assertFalse((home / ".config/fish/conf.d/ai.fish").exists())
                self._assert_fixture_cli(env)

    def test_bootstrap_rejects_archive_links(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root, unsafe_link=True)
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe or invalid", result.stderr)
            self.assertFalse((Path(env["HOME"]) / ".local/share/ai").exists())

    def test_bootstrap_download_failure_transcript(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            curl = root / "bin/curl"
            curl.write_text("#!/bin/sh\nexit 22\n", encoding="utf-8")
            curl.chmod(0o700)
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout,
                "Installing ai.\nDownloading the release... failed.\n",
            )
            self.assertEqual(result.stderr, "ai installer: release download failed\n")

    def test_bootstrap_rejects_tampered_archive(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive, installer, env = self._bootstrap_fixture(root)
            with archive.open("ab") as handle:
                handle.write(b"tampered")
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release checksum mismatch", result.stderr)
            self.assertFalse((Path(env["HOME"]) / ".local/share/ai/current").exists())

    def _install(
        self, installer: Path, env: dict[str, str], **extra: str
    ) -> subprocess.CompletedProcess[str]:
        invocation_env = env.copy()
        invocation_env.update(extra)
        return subprocess.run(
            ("bash", str(installer)),
            env=invocation_env,
            text=True,
            capture_output=True,
        )

    def _managed_snapshot(self, home: Path) -> dict[str, object]:
        release = home / ".local/share/ai/releases/9.8.7"
        launcher = home / ".local/bin/ai"
        fish = home / ".config/fish/conf.d/ai.fish"
        current = home / ".local/share/ai/current"
        files = {
            path.relative_to(release).as_posix(): (
                path.read_bytes(),
                path.stat().st_mode & 0o777,
            )
            for path in release.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        return {
            "files": files,
            "launcher": (launcher.read_bytes(), launcher.stat().st_mode & 0o777),
            "fish": (fish.read_bytes(), fish.stat().st_mode & 0o777),
            "current": current.readlink(),
        }

    def test_bootstrap_creates_exact_ownership_markers(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            result = self._install(installer, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            home = Path(env["HOME"])
            marker = home / ".local/share/ai/.ai-workstation-installation"
            launcher = home / ".local/bin/ai"
            fish = home / ".config/fish/conf.d/ai.fish"
            self.assertEqual(marker.read_text(), "ai-workstation installation format 1\n")
            self.assertFalse(marker.is_symlink())
            self.assertEqual(
                launcher.read_text().splitlines()[1], "# ai-workstation managed launcher format 1"
            )
            self.assertEqual(
                fish.read_text().splitlines()[0], "# ai-workstation managed fish PATH format 1"
            )
            self.assertIn("PYTHONDONTWRITEBYTECODE=1", launcher.read_text())

    def test_bootstrap_refuses_unrelated_install_roots_without_other_changes(self) -> None:
        self._require_arch_nonroot()
        cases = (
            "empty",
            "nonempty",
            "file",
            "special",
            "symlink",
            "invalid-marker",
            "marker-symlink",
            "marker-directory",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                home = Path(env["HOME"])
                install_root = home / ".local/share/ai"
                install_root.parent.mkdir(parents=True)
                if case == "file":
                    install_root.write_bytes(b"unrelated")
                elif case == "special":
                    os.mkfifo(install_root)
                elif case == "symlink":
                    target = root / "outside"
                    target.mkdir()
                    install_root.symlink_to(target, target_is_directory=True)
                else:
                    install_root.mkdir()
                    if case == "nonempty":
                        (install_root / "user-data").write_bytes(b"preserve")
                    elif case == "invalid-marker":
                        (install_root / ".ai-workstation-installation").write_text("wrong\n")
                    elif case == "marker-symlink":
                        target = root / "marker-target"
                        target.write_text("ai-workstation installation format 1\n")
                        (install_root / ".ai-workstation-installation").symlink_to(target)
                    elif case == "marker-directory":
                        (install_root / ".ai-workstation-installation").mkdir()
                before = sorted(
                    (
                        path.relative_to(home).as_posix(),
                        path.is_symlink(),
                        path.readlink() if path.is_symlink() else None,
                    )
                    for path in home.rglob("*")
                )
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(
                    "install root" in result.stderr
                    or "installation ownership marker" in result.stderr
                )
                after = sorted(
                    (
                        path.relative_to(home).as_posix(),
                        path.is_symlink(),
                        path.readlink() if path.is_symlink() else None,
                    )
                    for path in home.rglob("*")
                )
                self.assertEqual(after, before)

    def test_bootstrap_refuses_unrelated_launcher_and_fish_collisions(self) -> None:
        self._require_arch_nonroot()
        for kind in (
            "launcher-file",
            "launcher-symlink",
            "launcher-directory",
            "launcher-special",
            "fish-file",
            "fish-symlink",
            "fish-directory",
            "fish-special",
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                home = Path(env["HOME"])
                launcher = home / ".local/bin/ai"
                fish = home / ".config/fish/conf.d/ai.fish"
                path = launcher if kind.startswith("launcher") else fish
                path.parent.mkdir(parents=True)
                if kind.endswith("file"):
                    path.write_bytes(b"unrelated")
                    path.chmod(0o751)
                elif kind.endswith("symlink"):
                    target = root / "outside"
                    target.write_bytes(b"target")
                    path.symlink_to(target)
                elif kind.endswith("special"):
                    os.mkfifo(path)
                else:
                    path.mkdir()
                before_target = path.readlink().read_bytes() if path.is_symlink() else None
                before = path.lstat()
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(path_exists := (path.is_symlink() or path.exists()))
                self.assertTrue(path_exists)
                self.assertEqual(path.lstat().st_mode, before.st_mode)
                if path.is_symlink():
                    self.assertEqual(path.readlink().read_bytes(), before_target)
                elif path.is_file():
                    self.assertEqual(path.read_bytes(), b"unrelated")
                self.assertFalse((home / ".local/share/ai").exists())

    def test_bootstrap_refuses_wrong_owner_for_each_managed_boundary(self) -> None:
        self._require_arch_nonroot()
        for boundary in ("root", "marker", "launcher", "fish"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                home = Path(env["HOME"])
                install_root = home / ".local/share/ai"
                marker = install_root / ".ai-workstation-installation"
                launcher = home / ".local/bin/ai"
                fish = home / ".config/fish/conf.d/ai.fish"
                if boundary in {"root", "marker"}:
                    install_root.mkdir(parents=True)
                    marker.write_text("ai-workstation installation format 1\n")
                elif boundary == "launcher":
                    launcher.parent.mkdir(parents=True)
                    launcher.write_text("#!/bin/sh\n# ai-workstation managed launcher format 1\n")
                else:
                    fish.parent.mkdir(parents=True)
                    fish.write_text(
                        "# ai-workstation managed fish PATH format 1\n"
                        "fish_add_path --global --move $HOME/.local/bin\n"
                    )
                target = {
                    "root": install_root,
                    "marker": marker,
                    "launcher": launcher,
                    "fish": fish,
                }[boundary]
                env["FAKE_WRONG_OWNER_PATH"] = str(target)
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not owned", result.stderr)
                self.assertTrue(target.exists())

    def test_bootstrap_refuses_unsafe_current_paths(self) -> None:
        self._require_arch_nonroot()
        for kind in ("file", "directory", "outside-link", "dangling-link", "dangling-inside"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                self.assertEqual(self._install(installer, env).returncode, 0)
                home = Path(env["HOME"])
                current = home / ".local/share/ai/current"
                current.unlink()
                if kind == "file":
                    current.write_bytes(b"user")
                elif kind == "directory":
                    current.mkdir()
                elif kind == "outside-link":
                    outside = root / "outside"
                    outside.mkdir()
                    current.symlink_to(outside)
                elif kind == "dangling-inside":
                    current.symlink_to("releases/missing")
                else:
                    current.symlink_to(root / "missing")
                before = current.lstat()
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("current", result.stderr)
                self.assertEqual(current.lstat().st_mode, before.st_mode)

    def test_bootstrap_repairs_modified_missing_and_unexpected_same_version_content(self) -> None:
        self._require_arch_nonroot()
        for kind in ("modified", "missing", "unexpected"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                self.assertEqual(self._install(installer, env).returncode, 0)
                home = Path(env["HOME"])
                release = home / ".local/share/ai/releases/9.8.7"
                cli = release / "src/ai_setup/cli.py"
                expected = cli.read_bytes()
                if kind == "modified":
                    cli.write_bytes(expected + b"\n# stale\n")
                elif kind == "missing":
                    cli.unlink()
                else:
                    (release / "unexpected").write_bytes(b"stale")
                result = self._install(installer, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cli.read_bytes(), expected)
                self.assertFalse((release / "unexpected").exists())
                self._assert_fixture_cli(env)
                self.assertFalse(
                    any(".backup." in path.name or ".new." in path.name for path in home.rglob("*"))
                )

    def test_bootstrap_rejects_same_version_symlink_without_following_it(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            self.assertEqual(self._install(installer, env).returncode, 0)
            home = Path(env["HOME"])
            cli = home / ".local/share/ai/releases/9.8.7/src/ai_setup/cli.py"
            outside = root / "outside"
            outside.write_bytes(b"external")
            cli.unlink()
            cli.symlink_to(outside)
            result = self._install(installer, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe ownership, links, or special files", result.stderr)
            self.assertTrue(cli.is_symlink())
            self.assertEqual(outside.read_bytes(), b"external")

    def test_bootstrap_rejects_same_version_hard_links(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            self.assertEqual(self._install(installer, env).returncode, 0)
            home = Path(env["HOME"])
            release = home / ".local/share/ai/releases/9.8.7"
            source = release / "src/ai_setup/cli.py"
            linked = release / "hard-link"
            os.link(source, linked)
            result = self._install(installer, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe ownership, links, or special files", result.stderr)
            self.assertEqual(source.stat().st_ino, linked.stat().st_ino)

    def test_bootstrap_rolls_back_each_transaction_boundary(self) -> None:
        self._require_arch_nonroot()
        points = (
            "after_verified_extraction",
            "after_preserving_previous_release",
            "after_installing_release",
            "before_replacing_launcher",
            "after_replacing_launcher",
            "before_shell_integration",
            "after_shell_integration",
            "before_switching_current",
            "after_switching_current",
        )
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                self.assertEqual(self._install(installer, env).returncode, 0)
                home = Path(env["HOME"])
                release = home / ".local/share/ai/releases/9.8.7"
                (release / "src/ai_setup/cli.py").write_text(
                    "def main():\n print('ai 9.8.7')\n return 0\n# prior\n"
                )
                fish = home / ".config/fish/conf.d/ai.fish"
                fish.write_text(fish.read_text() + "# prior\n")
                before = self._managed_snapshot(home)
                result = self._install(
                    installer,
                    env,
                    AI_WORKSTATION_INSTALLER_TESTING="1",
                    AI_WORKSTATION_INSTALLER_TEST_SENTINEL="ai-bootstrap-test-only",
                    AI_WORKSTATION_TEST_FAILURE_POINT=point,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"injected failure at {point}", result.stderr)
                self.assertEqual(self._managed_snapshot(home), before)
                self._assert_fixture_cli(env)
                self.assertFalse(
                    any(".backup." in path.name or ".new." in path.name for path in home.rglob("*"))
                )

    def test_fresh_install_failures_leave_no_partial_managed_state(self) -> None:
        self._require_arch_nonroot()
        points = (
            "after_verified_extraction",
            "after_installing_release",
            "after_replacing_launcher",
            "after_shell_integration",
            "after_switching_current",
        )
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                home = Path(env["HOME"])
                result = self._install(
                    installer,
                    env,
                    AI_WORKSTATION_INSTALLER_TESTING="1",
                    AI_WORKSTATION_INSTALLER_TEST_SENTINEL="ai-bootstrap-test-only",
                    AI_WORKSTATION_TEST_FAILURE_POINT=point,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((home / ".local/share/ai").exists())
                self.assertFalse((home / ".local/bin/ai").exists())
                self.assertFalse((home / ".config/fish/conf.d/ai.fish").exists())

    def test_termination_signal_rolls_back_active_installation(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            self.assertEqual(self._install(installer, env).returncode, 0)
            home = Path(env["HOME"])
            release = home / ".local/share/ai/releases/9.8.7"
            (release / "src/ai_setup/cli.py").write_text(
                "def main():\n print('ai 9.8.7')\n return 0\n# prior\n"
            )
            before = self._managed_snapshot(home)
            invocation_env = env.copy()
            invocation_env.update(
                {
                    "AI_WORKSTATION_INSTALLER_TESTING": "1",
                    "AI_WORKSTATION_INSTALLER_TEST_SENTINEL": "ai-bootstrap-test-only",
                    "AI_WORKSTATION_TEST_PAUSE_POINT": "after_switching_current",
                }
            )
            process = subprocess.Popen(
                ("bash", str(installer)),
                env=invocation_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            output = process.stdout
            self.assertIsNotNone(output)
            if output is None:
                self.fail("installer stdout pipe was not created")
            while "test pause" not in output.readline():
                self.assertIsNone(process.poll(), "installer exited before its test pause")
            process.terminate()
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143)
            self.assertIn("rollback restored the prior installation", stderr)
            self.assertEqual(self._managed_snapshot(home), before)
            self._assert_fixture_cli(env)

    def test_bootstrap_serializes_installers_without_mutation(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            lock = (
                Path(os.environ.get("TMPDIR", "/tmp"))
                / f"ai-workstation-installer-{os.geteuid()}.lock"
            )
            lock.mkdir(mode=0o700)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
            (lock / "pid").chmod(0o600)
            try:
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("another installer is running", result.stderr)
                self.assertFalse((Path(env["HOME"]) / ".local/share/ai").exists())
            finally:
                (lock / "pid").unlink()
                lock.rmdir()
            self.assertEqual(self._install(installer, env).returncode, 0)

    def _kill_bootstrap_at(
        self,
        installer: Path,
        env: dict[str, str],
        point: str,
    ) -> None:
        invocation_env = env.copy()
        invocation_env.update(
            {
                "AI_WORKSTATION_INSTALLER_TESTING": "1",
                "AI_WORKSTATION_INSTALLER_TEST_SENTINEL": "ai-bootstrap-test-only",
                "AI_WORKSTATION_TEST_PAUSE_POINT": point,
            }
        )
        process = subprocess.Popen(
            ("bash", str(installer)),
            env=invocation_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIsNotNone(process.stdout)
        if process.stdout is None:
            self.fail("installer stdout pipe was not created")
        while True:
            line = process.stdout.readline()
            self.assertTrue(line, "installer exited before its crash boundary")
            if f"test pause at {point}" in line:
                break
        process.kill()
        self.assertEqual(process.wait(timeout=10), -9)
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def test_sigkill_recovery_for_fresh_and_replacement_transactions(self) -> None:
        self._require_arch_nonroot()
        points = (
            "after_journal_preparation",
            "after_preserving_previous_release",
            "after_installing_release",
            "after_replacing_launcher",
            "after_shell_integration",
            "after_switching_current",
            "after_recording_committed",
            "during_cleanup_after_commit",
        )
        for prior in (False, True):
            for point in points:
                with (
                    self.subTest(prior=prior, point=point),
                    tempfile.TemporaryDirectory() as raw,
                ):
                    root = Path(raw)
                    _, installer, env = self._bootstrap_fixture(root)
                    home = Path(env["HOME"])
                    if prior:
                        self.assertEqual(self._install(installer, env).returncode, 0)
                        release = home / ".local/share/ai/releases/9.8.7"
                        (release / "src/ai_setup/cli.py").write_text(
                            "def main():\n print('ai 9.8.7')\n return 0\n# prior\n"
                        )
                        fish = home / ".config/fish/conf.d/ai.fish"
                        fish.write_text(fish.read_text() + "# prior\n")
                    if not prior and point == "after_preserving_previous_release":
                        # A fresh install has no previous release-preservation mutation.
                        continue
                    self._kill_bootstrap_at(installer, env, point)
                    journal = home / ".local/share/ai/.ai-workstation-transaction.json"
                    self.assertTrue(journal.is_file())
                    self.assertFalse(journal.is_symlink())
                    self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
                    journal_data = json.loads(journal.read_text(encoding="utf-8"))
                    self.assertEqual(journal_data["schema"], 1)
                    self.assertRegex(journal_data["transaction_id"], r"\A[0-9a-f]{32}\Z")
                    recovered = self._install(installer, env)
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertIn("Recovered interrupted installer transaction.", recovered.stdout)
                    self._assert_fixture_cli(env)
                    self.assertFalse(journal.exists())
                    self.assertFalse(
                        any(
                            ".backup." in path.name or ".new." in path.name
                            for path in home.rglob("*")
                        )
                    )
                    if not prior and point == "after_journal_preparation":
                        self.assertEqual(self._install(installer, env).returncode, 0)

    def test_bootstrap_refuses_malformed_journals_and_unrecorded_remnants(self) -> None:
        self._require_arch_nonroot()
        invalid = (
            '{"schema":2}\n',
            '{"schema":1,"schema":1}\n',
            '{"schema":1,"staging":"../../outside"}\n',
            "not json\n",
        )
        for content in invalid:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                self.assertEqual(self._install(installer, env).returncode, 0)
                home = Path(env["HOME"])
                before = self._managed_snapshot(home)
                journal = home / ".local/share/ai/.ai-workstation-transaction.json"
                journal.write_text(content, encoding="utf-8")
                journal.chmod(0o600)
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("transaction journal", result.stderr)
                self.assertEqual(journal.read_text(), content)
                self.assertEqual(self._managed_snapshot(home), before)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            self.assertEqual(self._install(installer, env).returncode, 0)
            home = Path(env["HOME"])
            journal = home / ".local/share/ai/.ai-workstation-transaction.json"
            outside = root / "outside"
            outside.write_text("preserve", encoding="utf-8")
            journal.symlink_to(outside)
            result = self._install(installer, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("transaction journal", result.stderr)
            self.assertTrue(journal.is_symlink())
            self.assertEqual(outside.read_text(), "preserve")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            self.assertEqual(self._install(installer, env).returncode, 0)
            home = Path(env["HOME"])
            journal = home / ".local/share/ai/.ai-workstation-transaction.json"
            journal.write_text("{}\n", encoding="utf-8")
            journal.chmod(0o600)
            result = self._install(
                installer,
                env,
                AI_WORKSTATION_INSTALLER_TESTING="1",
                AI_WORKSTATION_INSTALLER_TEST_SENTINEL="ai-bootstrap-test-only",
                AI_WORKSTATION_TEST_WRONG_OWNER_JOURNAL=str(journal),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("user-owned regular file", result.stderr)
            self.assertTrue(journal.is_file())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            self.assertEqual(self._install(installer, env).returncode, 0)
            home = Path(env["HOME"])
            remnant = home / ".local/share/ai/releases/.9.8.7.new.0123456789abcdef0123456789abcdef"
            remnant.mkdir()
            result = self._install(installer, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized installer remnants", result.stderr)
            self.assertTrue(remnant.is_dir())

    def test_reconciliation_refuses_missing_backup_and_unrelated_current(self) -> None:
        self._require_arch_nonroot()
        for corruption in ("missing-release-backup", "unrelated-current"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, installer, env = self._bootstrap_fixture(root)
                self.assertEqual(self._install(installer, env).returncode, 0)
                home = Path(env["HOME"])
                release = home / ".local/share/ai/releases/9.8.7"
                (release / "src/ai_setup/cli.py").write_text(
                    "def main():\n print('ai 9.8.7')\n return 0\n# prior\n"
                )
                point = (
                    "after_installing_release"
                    if corruption == "missing-release-backup"
                    else "after_switching_current"
                )
                self._kill_bootstrap_at(installer, env, point)
                journal = home / ".local/share/ai/.ai-workstation-transaction.json"
                data = json.loads(journal.read_text(encoding="utf-8"))
                if corruption == "missing-release-backup":
                    backup = home / ".local/share/ai" / data["release_backup"]
                    for path in sorted(backup.rglob("*"), reverse=True):
                        if path.is_file():
                            path.unlink()
                        else:
                            path.rmdir()
                    backup.rmdir()
                else:
                    current = home / ".local/share/ai/current"
                    current.unlink()
                    current.write_text("unrelated", encoding="utf-8")
                before_journal = journal.read_bytes()
                result = self._install(installer, env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("transaction journal", result.stderr)
                self.assertEqual(journal.read_bytes(), before_journal)

    def test_installer_durability_contract_uses_fsync_without_broad_sync(self) -> None:
        source = Path("bootstrap/install.in").read_text(encoding="utf-8")
        self.assertIn('readonly JOURNAL_NAME=".ai-workstation-transaction.json"', source)
        self.assertIn("os.fsync", source)
        self.assertIn("os.replace(temporary, journal)", source)
        self.assertNotRegex(source, r"(?m)^\s*sync(?:\s|$)")
        self.assertNotRegex(source, r"rm\s+-rf[^\n]*\*")
        self.assertNotRegex(source, r"find[^\n]*-delete")

    def test_bootstrap_rejects_tampered_embedded_hash(self) -> None:
        self._require_arch_nonroot()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, installer, env = self._bootstrap_fixture(root)
            content = installer.read_text(encoding="utf-8")
            content = re.sub(
                r'(?m)^readonly EXPECTED_SHA256="[0-9a-f]{64}"$',
                'readonly EXPECTED_SHA256="' + "0" * 64 + '"',
                content,
            )
            installer.write_text(content, encoding="utf-8")
            result = subprocess.run(
                ("bash", str(installer)), env=env, text=True, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release checksum mismatch", result.stderr)
