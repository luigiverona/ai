from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_setup.config.files import (
    ExistingFilePolicy,
    inspect_managed_file,
    replace_managed_file,
)
from ai_setup.errors import ValidationError
from ai_setup.identity import IDENTITY

SUPPORTED_SHELLS = frozenset({"fish", "bash", "zsh"})


@dataclass(frozen=True, slots=True)
class ShellInfo:
    name: str
    executable: Path
    source: str


@dataclass(frozen=True, slots=True)
class ProcessShell:
    name: str
    interactive: bool


@dataclass(frozen=True, slots=True)
class PathUpdate:
    path: Path
    changed: bool
    current_session_has_path: bool

    @property
    def new_session_required(self) -> bool:
        return self.changed and not self.current_session_has_path


def _process_shell(pid: int) -> ProcessShell | None:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip().lstrip("-")
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        args = [part.decode(errors="replace") for part in raw if part]
    except OSError:
        return None
    name = Path(comm).name
    if name not in SUPPORTED_SHELLS:
        return None
    noninteractive = any(
        arg == "-c" or (arg.startswith("-") and "c" in arg[1:]) for arg in args[1:]
    )
    explicitly_interactive = any(
        arg == "-i" or (arg.startswith("-") and "i" in arg[1:]) for arg in args[1:]
    )
    try:
        fd = os.open(f"/proc/{pid}/fd/0", os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        has_tty = False
    else:
        try:
            has_tty = os.isatty(fd)
        finally:
            os.close(fd)
    return ProcessShell(name, explicitly_interactive or (not noninteractive and has_tty))


def _parent_pid(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        value = next(
            line.split(":", 1)[1].strip()
            for line in status.splitlines()
            if line.startswith("PPid:")
        )
        parent = int(value)
    except (OSError, StopIteration, ValueError):
        return None
    return parent or None


def process_ancestry(start_pid: int | None = None) -> tuple[ProcessShell, ...]:
    pid: int | None = os.getppid() if start_pid is None else start_pid
    result: list[ProcessShell] = []
    visited: set[int] = set()
    while pid and pid not in visited and len(visited) < 64:
        visited.add(pid)
        candidate = _process_shell(pid)
        if candidate:
            result.append(candidate)
        pid = _parent_pid(pid)
    return tuple(result)


def detect_shell(
    *,
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
    ancestry: tuple[ProcessShell, ...] | None = None,
    login_shell: str | None = None,
) -> ShellInfo:
    environment = dict(os.environ if env is None else env)
    target_uid = os.getuid() if uid is None else uid
    processes = process_ancestry() if ancestry is None else ancestry
    for process in processes:
        if process.interactive and process.name in SUPPORTED_SHELLS:
            return ShellInfo(process.name, Path("/usr/bin") / process.name, "interactive process")
    if login_shell is None:
        try:
            login_shell = pwd.getpwuid(target_uid).pw_shell
        except KeyError:
            login_shell = ""
    candidates = ((login_shell, "login account"), (environment.get("SHELL", ""), "environment"))
    for candidate, source in candidates:
        path = Path(candidate)
        if path.name in SUPPORTED_SHELLS:
            executable = path if path.is_absolute() else Path("/usr/bin") / path.name
            return ShellInfo(path.name, executable, source)
    for process in processes:
        if process.name in SUPPORTED_SHELLS:
            return ShellInfo(process.name, Path("/usr/bin") / process.name, "process fallback")
    raise ValueError("supported interactive shell not detected (fish, bash, or zsh required)")


def startup_path(home: Path, shell: ShellInfo) -> Path:
    if shell.name == "fish":
        return IDENTITY.fish_configuration(home)
    if shell.name == "zsh":
        return home / ".zshrc"
    return home / ".bashrc"


def _path_line(shell: ShellInfo) -> str:
    if shell.name == "fish":
        return "fish_add_path --global --move $HOME/.local/bin"
    return 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'


def current_path_contains(home: Path, env: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    expected = str(home / ".local/bin")
    return expected in environment.get("PATH", "").split(":")


def configure_path(
    home: Path,
    shell: ShellInfo,
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> PathUpdate:
    path = startup_path(home, shell)
    line = _path_line(shell)
    owner_uid = os.getuid()
    snapshot = inspect_managed_file(
        trusted_root=home,
        target=path,
        owner_uid=owner_uid,
    )
    existing = snapshot.content.decode("utf-8") if snapshot is not None else ""
    current = current_path_contains(home, env)
    if line in existing.splitlines():
        content = existing
        changed = False
    else:
        content = existing
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"\n{IDENTITY.shell_managed_marker}\n" + line + "\n"
        changed = True
    if not dry_run:
        replaced = replace_managed_file(
            trusted_root=home,
            target=path,
            content=content,
            mode=0o644,
            owner_uid=owner_uid,
            expected=snapshot,
            existing_policy=(
                ExistingFilePolicy.EXACT_CONTENT
                if shell.name == "fish"
                else ExistingFilePolicy.USER_OWNED
            ),
            directory_mode=0o755,
        )
        changed = changed or replaced
    return PathUpdate(path, changed, current)


def path_configured(home: Path, shell: ShellInfo) -> bool:
    path = startup_path(home, shell)
    try:
        snapshot = inspect_managed_file(
            trusted_root=home,
            target=path,
            owner_uid=os.getuid(),
        )
    except ValidationError:
        return False
    if snapshot is None:
        return False
    return _path_line(shell) in snapshot.content.decode("utf-8").splitlines()
