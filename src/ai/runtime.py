from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AiError


@dataclass
class Runtime:
    dry_run: bool = False
    verbose: bool = False
    home: Path = field(default_factory=Path.home)
    changes: list[str] = field(default_factory=list)

    def run(self, argv: list[str], *, check: bool = True, capture: bool = True,
            input: str | None = None, env: dict[str, str] | None = None,
            mutate: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if self.verbose:
            print("+ " + shlex.join(argv))
        if mutate and self.dry_run:
            return subprocess.CompletedProcess(argv, 0, "", "")
        merged = os.environ.copy()
        if env:
            merged.update(env)
        try:
            result = subprocess.run(argv, cwd=cwd, env=merged, input=input, text=True,
                                    capture_output=capture, check=False)
        except FileNotFoundError:
            if check:
                raise AiError(f"command not found: {argv[0]}") from None
            return subprocess.CompletedProcess(argv, 127, "", f"command not found: {argv[0]}")
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise AiError(f"{shlex.join(argv)}: {detail}")
        return result

    def sudo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self.run(["sudo", "--", *argv], mutate=True)

    def changed(self, description: str) -> None:
        self.changes.append(description)

    def require_command(self, name: str, component: str) -> None:
        from shutil import which
        if which(name) is None:
            raise AiError(f"{component}: required command not found: {name}")

    def command_exists(self, name: str) -> bool:
        from shutil import which
        return which(name) is not None

    def atomic_write(self, path: Path, data: str, mode: int = 0o600) -> None:
        ensure_safe_parent(path.parent, self.home)
        reject_unsafe_existing(path)
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def reject_unsafe_existing(path: Path, *, allow_symlink: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) and not allow_symlink:
        raise AiError(f"unsafe managed path is a symlink: {path}")
    if not (stat.S_ISREG(info.st_mode) or (allow_symlink and stat.S_ISLNK(info.st_mode))):
        raise AiError(f"unsafe managed path type: {path}")
    if info.st_uid != os.getuid():
        raise AiError(f"managed path is not owned by current user: {path}")


def ensure_safe_parent(path: Path, home: Path) -> None:
    home = home.absolute()
    path = path.absolute()
    try:
        path.relative_to(home)
    except ValueError as exc:
        raise AiError(f"managed path is outside home: {path}") from exc
    current = path
    while current != home and current != current.parent:
        try:
            info = current.lstat()
        except FileNotFoundError:
            current = current.parent
            continue
        if stat.S_ISLNK(info.st_mode):
            raise AiError(f"unsafe parent symlink: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise AiError(f"unsafe parent path type: {current}")
        if info.st_uid != os.getuid():
            raise AiError(f"managed parent is not owned by current user: {current}")
        current = current.parent
