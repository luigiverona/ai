from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ai_setup.config.files import write_workspace_file
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import Command, CommandRunner
from ai_setup.packages.aur_source import YAY_BIN_SOURCE, srcinfo_identity, tracked_tree_sha256


@dataclass(frozen=True, slots=True)
class YayReadiness:
    dependency_satisfied: bool
    executable_runnable: bool

    @property
    def ready(self) -> bool:
        return self.dependency_satisfied and self.executable_runnable


def _xdg_home(home: Path, variable: str, fallback: str) -> Path:
    configured = os.environ.get(variable)
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return home / fallback


def flatpak_user_root(home: Path) -> Path:
    return _xdg_home(home, "XDG_DATA_HOME", ".local/share") / "flatpak"


def flatpak_state_exists(home: Path) -> bool:
    root = flatpak_user_root(home)
    repository = root / "repo"
    configuration = repository / "config"
    return (
        not root.is_symlink()
        and root.is_dir()
        and not repository.is_symlink()
        and repository.is_dir()
        and not configuration.is_symlink()
        and configuration.is_file()
    )


@contextmanager
def probe_environment(
    home: Path,
    *,
    preserve_data: bool = False,
) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="ai-inspection-") as raw:
        root = Path(raw)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        temporary = root / "tmp"
        temporary.mkdir(mode=0o700)
        data_home = _xdg_home(home, "XDG_DATA_HOME", ".local/share")
        yield {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(data_home if preserve_data else root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "TMPDIR": str(temporary),
        }


def _owned_yay_executable(runner: CommandRunner) -> str | None:
    executable = shutil.which("yay")
    if executable is None:
        return None
    try:
        owned = runner.run(Command(("pacman", "-Qo", "--", executable), mutate=False), check=False)
    except FileNotFoundError:
        return None
    return executable if owned.returncode == 0 else None


def yay_readiness(runner: CommandRunner, home: Path | None = None) -> YayReadiness:
    try:
        dependency = (
            runner.run(Command(("pacman", "-T", "yay"), mutate=False), check=False).returncode == 0
        )
    except FileNotFoundError:
        dependency = False
    executable_path = _owned_yay_executable(runner) if dependency else None
    if executable_path is None:
        return YayReadiness(dependency, False)
    try:
        with probe_environment(home or Path.home()) as environment:
            executable = (
                runner.run(
                    Command((executable_path, "--version"), env=environment, mutate=False),
                    check=False,
                ).returncode
                == 0
            )
    except FileNotFoundError:
        executable = False
    return YayReadiness(dependency, executable)


@dataclass(frozen=True, slots=True)
class FlathubReadiness:
    flatpak_available: bool
    configured: bool


def flathub_readiness(
    runner: CommandRunner,
    home: Path,
    *,
    flatpak_executable: str | None = None,
) -> FlathubReadiness:
    if flatpak_executable is None and shutil.which("flatpak") is None:
        return FlathubReadiness(False, False)
    if not flatpak_state_exists(home):
        return FlathubReadiness(True, False)
    with probe_environment(home, preserve_data=True) as environment:
        result = runner.run(
            Command(
                ("flatpak", "remotes", "--user", "--columns=name"),
                env=environment,
                mutate=False,
            ),
            check=False,
        )
    return FlathubReadiness(True, "flathub" in result.stdout.split())


class PacmanManager:
    def __init__(self, runner: CommandRunner, workspace: Path | None = None) -> None:
        self.runner = runner
        self.workspace = workspace

    def _command(self, argv: tuple[str, ...], packages: tuple[str, ...]) -> Command:
        log_path = self.workspace / "logs/pacman.log" if self.workspace else None
        return Command(
            argv,
            failure_component="Package installation",
            failure_operation="install packages",
            failure_packages=packages,
            log_path=log_path,
        )

    def full_update(self) -> bool:
        result = self.runner.run(
            Command(
                ("sudo", "pacman", "-Syu", "--noconfirm", "--needed"),
                env={"LC_ALL": "C"},
                failure_component="Package installation",
                failure_operation="install packages",
                log_path=self.workspace / "logs/pacman.log" if self.workspace else None,
            )
        )
        combined = (result.stdout + result.stderr).lower()
        return "there is nothing to do" not in combined

    def install(self, packages: Iterable[str]) -> None:
        names = tuple(sorted(set(packages)))
        if names:
            self.runner.run(
                self._command(("sudo", "pacman", "-S", "--needed", "--noconfirm", *names), names)
            )


class AurManager:
    def __init__(
        self,
        runner: CommandRunner,
        workspace: Path,
        makepkg_config_source: Path = Path("/etc/makepkg.conf"),
    ) -> None:
        self.runner = runner
        self.workspace = workspace
        self.makepkg_config_source = makepkg_config_source

    def _makepkg_config(self) -> Path:
        try:
            content = self.makepkg_config_source.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError("aur", "configure makepkg", str(exc)) from exc
        content += """

# ai builds only requested top-level packages; debug packages are not release requirements.
for _ai_index in "${!OPTIONS[@]}"; do
  case "${OPTIONS[_ai_index]}" in
    debug|!debug) OPTIONS[_ai_index]=!debug ;;
  esac
done
"""
        path = self.workspace / "state/makepkg.conf"
        write_workspace_file(path, content, 0o600)
        return path

    def bootstrap_yay(self) -> None:
        if os.geteuid() == 0:
            raise ValidationError("aur", "bootstrap yay", "makepkg must not run as root")
        clone = self.workspace / "aur" / "yay-bin"
        makepkg_config = self._makepkg_config()
        self.runner.run(Command(("git", "init", str(clone))))
        self.runner.run(
            Command(("git", "-C", str(clone), "remote", "add", "origin", YAY_BIN_SOURCE.repository))
        )
        self.runner.run(
            Command(
                (
                    "git",
                    "-C",
                    str(clone),
                    "fetch",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    YAY_BIN_SOURCE.commit,
                )
            )
        )
        self.runner.run(
            Command(("git", "-C", str(clone), "checkout", "--detach", YAY_BIN_SOURCE.commit))
        )
        head = self.runner.run(
            Command(("git", "-C", str(clone), "rev-parse", "HEAD"), mutate=False)
        )
        remotes = self.runner.run(Command(("git", "-C", str(clone), "remote"), mutate=False))
        origin = self.runner.run(
            Command(("git", "-C", str(clone), "remote", "get-url", "origin"), mutate=False)
        )
        submodules = self.runner.run(
            Command(("git", "-C", str(clone), "submodule", "status", "--recursive"), mutate=False)
        )
        index = self.runner.run(
            Command(("git", "-C", str(clone), "ls-files", "--stage", "-z"), mutate=False)
        )
        status = self.runner.run(
            Command(
                (
                    "git",
                    "-C",
                    str(clone),
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--ignored",
                    "-z",
                ),
                mutate=False,
            )
        )
        if not self.runner.dry_run:
            if head.stdout.strip() != YAY_BIN_SOURCE.commit:
                raise ValidationError("aur", "validate yay", "unexpected AUR commit")
            if remotes.stdout.split() != ["origin"]:
                raise ValidationError("aur", "validate yay", "unexpected AUR remotes")
            if origin.stdout.strip() != YAY_BIN_SOURCE.repository:
                raise ValidationError("aur", "validate yay", "unexpected AUR repository origin")
            if submodules.stdout.strip():
                raise ValidationError("aur", "validate yay", "AUR submodules are not allowed")
            digest = tracked_tree_sha256(clone, index.stdout, status.stdout)
            if digest != YAY_BIN_SOURCE.tree_sha256:
                raise ValidationError("aur", "validate yay", "unexpected AUR tracked-tree digest")
            try:
                package_base, package_names = srcinfo_identity(
                    (clone / ".SRCINFO").read_text(encoding="utf-8")
                )
            except OSError as exc:
                raise ValidationError("aur", "validate yay", "could not read .SRCINFO") from exc
            if (
                package_base != YAY_BIN_SOURCE.package_base
                or package_names != YAY_BIN_SOURCE.package_names
            ):
                raise ValidationError("aur", "validate yay", "unexpected AUR package metadata")
        metadata = self.runner.run(
            Command(
                ("makepkg", "--config", str(makepkg_config), "--printsrcinfo"),
                cwd=clone,
                mutate=False,
            )
        )
        if not self.runner.dry_run:
            package_base, package_names = srcinfo_identity(metadata.stdout)
            if (
                package_base != YAY_BIN_SOURCE.package_base
                or package_names != YAY_BIN_SOURCE.package_names
            ):
                raise ValidationError("aur", "validate yay", "unexpected AUR package metadata")
        self.runner.run(
            Command(
                ("makepkg", "--config", str(makepkg_config), "--cleanbuild", "--noconfirm"),
                cwd=clone,
            )
        )
        package_list = self.runner.run(
            Command(
                ("makepkg", "--config", str(makepkg_config), "--packagelist"),
                cwd=clone,
                mutate=False,
            )
        )
        candidates = tuple(
            line.strip() for line in package_list.stdout.splitlines() if line.strip()
        )
        artifacts = tuple(
            artifact
            for artifact in candidates
            if Path(artifact).name.startswith("yay-bin-")
            and not Path(artifact).name.startswith("yay-bin-debug-")
        )
        if not artifacts and not self.runner.dry_run:
            raise ValidationError("aur", "bootstrap yay", "makepkg did not produce a package")
        if artifacts:
            self.runner.run(
                Command(
                    ("sudo", "pacman", "-U", "--noconfirm", *artifacts),
                    failure_component="AUR bootstrap",
                    failure_operation="install yay",
                    failure_packages=("yay-bin",),
                    log_path=self.workspace / "logs/aur-bootstrap.log",
                )
            )
        if not self.runner.dry_run and not yay_readiness(self.runner).ready:
            raise ValidationError(
                "aur",
                "verify yay",
                "installed AUR helper is not runnable or does not satisfy the yay dependency",
            )

    def install(self, packages: Iterable[str]) -> None:
        names = tuple(sorted(set(packages)))
        if not names:
            return
        makepkg_config = self._makepkg_config()
        for name in names:
            self.runner.run(
                Command(
                    (
                        "yay",
                        "-S",
                        "--needed",
                        "--noconfirm",
                        "--builddir",
                        str(self.workspace / "aur"),
                        "--makepkgconf",
                        str(makepkg_config),
                        name,
                    ),
                    failure_component="AUR installation",
                    failure_operation="install packages",
                    failure_packages=(name,),
                    log_path=self.workspace / "logs/aur.log",
                )
            )


class FlatpakManager:
    REMOTE = "https://dl.flathub.org/repo/flathub.flatpakrepo"

    def __init__(self, runner: CommandRunner, home: Path | None = None) -> None:
        self.runner = runner
        self.home = home or Path.home()

    def ensure_flathub(self, readiness: FlathubReadiness | None = None) -> bool:
        state = readiness or flathub_readiness(self.runner, self.home)
        if not state.flatpak_available:
            raise ValidationError(
                "Flatpak",
                "inspect Flathub remote",
                "Flatpak is not installed",
            )
        changed = not state.configured
        if changed:
            self.runner.run(
                Command(
                    ("flatpak", "remote-add", "--user", "--if-not-exists", "flathub", self.REMOTE)
                )
            )
        self.runner.run(Command(("flatpak", "update", "--user", "--appstream", "--noninteractive")))
        return changed

    def install(self, applications: Iterable[str]) -> None:
        names = tuple(sorted(set(applications)))
        if names:
            self.runner.run(
                Command(
                    (
                        "flatpak",
                        "install",
                        "--user",
                        "--noninteractive",
                        "--or-update",
                        "flathub",
                        *names,
                    )
                )
            )
