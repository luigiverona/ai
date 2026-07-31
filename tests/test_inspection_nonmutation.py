from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_setup.execution.runner import Command, CommandResult
from ai_setup.models import Package, PackageKind, Source
from ai_setup.packages.managers import flathub_readiness, yay_readiness
from ai_setup.planning.state import StateInspector
from tests.helpers import FakeRunner


def filesystem_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*"), key=lambda item: os.fsencode(item.relative_to(root))):
        metadata = path.lstat()
        kind = (
            "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "regular"
            if stat.S_ISREG(metadata.st_mode)
            else "other"
        )
        entries.append(
            (
                path.relative_to(root).as_posix(),
                kind,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_size,
                metadata.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest() if kind == "regular" else None,
                os.readlink(path) if kind == "symlink" else None,
            )
        )
    return tuple(entries)


class StateCreatingRunner(FakeRunner):
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        super().__init__(responses)
        self.probe_roots: list[Path] = []

    def run(self, command: Command, *, check: bool = True) -> CommandResult:
        if command.env and command.argv[0] in {"/usr/bin/yay", "flatpak"}:
            for variable in (
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_STATE_HOME",
                "XDG_RUNTIME_DIR",
            ):
                root = Path(command.env[variable])
                root.mkdir(parents=True, exist_ok=True)
                (root / "probe-state").write_text("ephemeral\n", encoding="utf-8")
                self.probe_roots.append(root)
        return super().run(command, check=check)


class InspectionNonMutationTests(unittest.TestCase):
    COMMANDS = (
        ("root", "--dry-run"),
        ("setup", "setup", "--dry-run"),
        ("apps", "apps", "--dry-run"),
        ("apps-browser", "apps", "browser", "--dry-run"),
        ("git", "git", "--dry-run"),
        ("github", "github", "--dry-run"),
        ("ssh", "ssh", "--dry-run"),
        ("codex", "codex", "--dry-run"),
        ("status", "status"),
    )

    def _fake_commands(self, root: Path, *, provider_ready: bool) -> Path:
        binary_root = root / "bin"
        binary_root.mkdir()
        marker = root / "unexpected-persistent-probe"
        pacman = """#!/bin/sh
case "$1 $2" in
  "-T yay"|"-Qo --") exit %s ;;
  *) exit 1 ;;
esac
""" % ("0" if provider_ready else "1")
        scripts = {
            "pacman": pacman,
            "yay": f"""#!/bin/sh
mkdir -p "$XDG_CACHE_HOME/yay" "$XDG_CONFIG_HOME/yay"
printf touched >"{marker}"
printf 'yay v13.0.1\\n'
""",
            "flatpak": """#!/bin/sh
mkdir -p "$XDG_CACHE_HOME/flatpak" "$XDG_STATE_HOME/flatpak"
case "$1" in
  info) exit 0 ;;
  remotes) printf 'flathub\\n'; exit 0 ;;
esac
exit 1
""",
            "git": "#!/bin/sh\nexit 1\n",
            "gh": "#!/bin/sh\nexit 1\n",
            "ssh": "#!/bin/sh\nexit 1\n",
        }
        for name, content in scripts.items():
            path = binary_root / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o700)
        return binary_root

    def _run_matrix(self, *, existing: bool) -> None:
        repository = Path(__file__).resolve().parents[1]
        for label, *arguments in self.COMMANDS:
            with self.subTest(command=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                home = root / "home"
                home.mkdir()
                runtime = root / "runtime"
                runtime.mkdir(mode=0o700)
                binary_root = self._fake_commands(root, provider_ready=False)
                if existing:
                    flatpak_repository = home / ".local/share/flatpak/repo"
                    flatpak_repository.mkdir(parents=True)
                    (flatpak_repository / "config").write_text(
                        "[core]\nrepo_version=1\n", encoding="utf-8"
                    )
                    unrelated = home / ".config/unrelated.conf"
                    unrelated.parent.mkdir()
                    unrelated.write_text("preserve\n", encoding="utf-8")
                    cache = home / ".cache/unrelated"
                    cache.parent.mkdir()
                    cache.write_text("preserve\n", encoding="utf-8")
                before = filesystem_snapshot(home)
                environment = os.environ.copy()
                environment.update(
                    {
                        "HOME": str(home),
                        "PATH": f"{binary_root}:/usr/bin:/bin",
                        "PYTHONPATH": str(repository / "src"),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "AI_WORKSTATION_CATALOG_ROOT": str(repository),
                        "XDG_RUNTIME_DIR": str(runtime),
                    }
                )
                for variable in (
                    "XDG_CONFIG_HOME",
                    "XDG_CACHE_HOME",
                    "XDG_DATA_HOME",
                    "XDG_STATE_HOME",
                ):
                    environment.pop(variable, None)
                result = subprocess.run(
                    (sys.executable, "-m", "ai_setup", *arguments),
                    cwd=repository,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1 if label == "status" else 0, result.stderr)
                self.assertEqual(filesystem_snapshot(home), before)
                self.assertFalse((root / "unexpected-persistent-probe").exists())

    def test_every_public_dry_run_and_status_preserves_empty_home(self) -> None:
        self._run_matrix(existing=False)

    def test_every_public_dry_run_and_status_preserves_existing_home(self) -> None:
        self._run_matrix(existing=True)

    def test_yay_provider_probe_is_owned_accurate_and_ephemeral(self) -> None:
        dependency = ("pacman", "-T", "yay")
        owner = ("pacman", "-Qo", "--", "/usr/bin/yay")
        version = ("/usr/bin/yay", "--version")
        responses = {
            dependency: CommandResult(dependency, 0, "", ""),
            owner: CommandResult(owner, 0, "yay-bin owns /usr/bin/yay\n", ""),
            version: CommandResult(version, 0, "yay v13.0.1\n", ""),
        }
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "home"
            home.mkdir()
            before = filesystem_snapshot(home)
            runner = StateCreatingRunner(responses)
            state = yay_readiness(runner, home)  # type: ignore[arg-type]
            self.assertTrue(state.ready)
            self.assertEqual(filesystem_snapshot(home), before)
            self.assertTrue(runner.probe_roots)
            self.assertTrue(all(not path.exists() for path in runner.probe_roots))
            command = next(item for item in runner.commands if item.argv == version)
            self.assertEqual(command.env["HOME"], str(home))
            self.assertNotEqual(command.env["XDG_CACHE_HOME"], str(home / ".cache"))
            self.assertNotEqual(command.env["XDG_CONFIG_HOME"], str(home / ".config"))

    def test_absent_unowned_and_broken_yay_providers_fail_closed(self) -> None:
        dependency = ("pacman", "-T", "yay")
        owner = ("pacman", "-Qo", "--", "/usr/bin/yay")
        version = ("/usr/bin/yay", "--version")
        cases = (
            ({dependency: CommandResult(dependency, 1, "", "")}, (False, False), (dependency,)),
            (
                {
                    dependency: CommandResult(dependency, 0, "", ""),
                    owner: CommandResult(owner, 1, "", ""),
                },
                (True, False),
                (dependency, owner),
            ),
            (
                {
                    dependency: CommandResult(dependency, 0, "", ""),
                    owner: CommandResult(owner, 0, "", ""),
                    version: CommandResult(version, 1, "", "broken"),
                },
                (True, False),
                (dependency, owner, version),
            ),
        )
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            for responses, expected, commands in cases:
                runner = FakeRunner(responses)
                state = yay_readiness(runner, home)  # type: ignore[arg-type]
                self.assertEqual((state.dependency_satisfied, state.executable_runnable), expected)
                self.assertEqual(tuple(command.argv for command in runner.commands), commands)

    def test_absent_flatpak_state_skips_commands_and_remains_absent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            runner = FakeRunner()
            package = Package(
                Source.FLATPAK,
                "org.example.Application",
                "Application",
                "test",
                PackageKind.APPLICATION,
            )
            with patch("ai_setup.packages.managers.shutil.which", return_value="/usr/bin/flatpak"):
                self.assertFalse(StateInspector(runner, home).package_installed(package))  # type: ignore[arg-type]
                readiness = flathub_readiness(runner, home)  # type: ignore[arg-type]
            self.assertTrue(readiness.flatpak_available)
            self.assertFalse(readiness.configured)
            self.assertEqual(runner.commands, [])
            self.assertFalse((home / ".local/share/flatpak").exists())

    def test_existing_flatpak_applications_and_flathub_use_ephemeral_probe_state(self) -> None:
        info = ("flatpak", "info", "--user", "org.example.Application")
        remotes = ("flatpak", "remotes", "--user", "--columns=name")
        responses = {
            info: CommandResult(info, 0, "", ""),
            remotes: CommandResult(remotes, 0, "flathub\n", ""),
        }
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n", encoding="utf-8")
            before = filesystem_snapshot(home)
            runner = StateCreatingRunner(responses)
            package = Package(
                Source.FLATPAK,
                "org.example.Application",
                "Application",
                "test",
                PackageKind.APPLICATION,
            )
            self.assertTrue(StateInspector(runner, home).package_installed(package))  # type: ignore[arg-type]
            with patch("ai_setup.packages.managers.shutil.which", return_value="/usr/bin/flatpak"):
                readiness = flathub_readiness(runner, home)  # type: ignore[arg-type]
            self.assertTrue(readiness.configured)
            self.assertEqual(filesystem_snapshot(home), before)
            self.assertTrue(all(not path.exists() for path in runner.probe_roots))
            for command in runner.commands:
                if command.argv[0] == "flatpak":
                    self.assertEqual(command.env["XDG_DATA_HOME"], str(home / ".local/share"))
                    self.assertNotEqual(command.env["XDG_CACHE_HOME"], str(home / ".cache"))
