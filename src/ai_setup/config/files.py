from __future__ import annotations

import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ai_setup.errors import ValidationError


class ExistingFilePolicy(Enum):
    USER_OWNED = "user-owned"
    EXACT_CONTENT = "exact managed content"


@dataclass(frozen=True, slots=True)
class ManagedFileSnapshot:
    content: bytes
    mode: int
    device: int
    inode: int
    modified_ns: int


def _error(target: Path, operation: str, reason: str) -> ValidationError:
    return ValidationError("managed file", f"{operation} {target}", reason)


def _relative_target(trusted_root: Path, target: Path) -> tuple[str, ...]:
    if not trusted_root.is_absolute() or not target.is_absolute():
        raise _error(target, "validate", "trusted root and target must be absolute")
    if "\0" in str(trusted_root) or "\0" in str(target):
        raise _error(target, "validate", "path contains a NUL byte")
    try:
        relative = target.relative_to(trusted_root)
    except ValueError as exc:
        raise _error(target, "validate", "target is outside the trusted root") from exc
    if relative == Path(".") or not relative.parts:
        raise _error(target, "validate", "target must not equal the trusted root")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise _error(target, "validate", "target contains an unsafe relative component")
    return relative.parts


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _validate_directory(stat_result: os.stat_result, path: Path, owner_uid: int) -> None:
    if not stat.S_ISDIR(stat_result.st_mode):
        raise _error(path, "validate", "ancestor is not a directory")
    if stat_result.st_uid != owner_uid:
        raise _error(path, "validate", f"ancestor is not owned by uid {owner_uid}")


def fsync_directory(fd: int, path: Path) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise _error(path, "fsync directory", str(exc)) from exc


def _open_parent(
    *,
    trusted_root: Path,
    target: Path,
    owner_uid: int,
    create: bool,
    directory_mode: int,
) -> tuple[int, str]:
    parts = _relative_target(trusted_root, target)
    try:
        root_stat = trusted_root.lstat()
    except OSError as exc:
        raise _error(trusted_root, "validate root", str(exc)) from exc
    _validate_directory(root_stat, trusted_root, owner_uid)
    try:
        current_fd = os.open(trusted_root, _directory_flags())
    except OSError as exc:
        raise _error(trusted_root, "open root", str(exc)) from exc
    current_path = trusted_root
    try:
        _validate_directory(os.fstat(current_fd), current_path, owner_uid)
        for part in parts[:-1]:
            next_path = current_path / part
            try:
                item = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return -1, parts[-1]
                try:
                    os.mkdir(part, directory_mode, dir_fd=current_fd)
                    fsync_directory(current_fd, current_path)
                    item = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                except OSError as exc:
                    raise _error(next_path, "create directory", str(exc)) from exc
                _validate_directory(item, next_path, owner_uid)
                if stat.S_IMODE(item.st_mode) != directory_mode:
                    raise _error(
                        next_path, "verify directory", "created mode is not exact"
                    ) from None
            except OSError as exc:
                raise _error(next_path, "inspect ancestor", str(exc)) from exc
            _validate_directory(item, next_path, owner_uid)
            try:
                next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise _error(next_path, "open ancestor", str(exc)) from exc
            try:
                _validate_directory(os.fstat(next_fd), next_path, owner_uid)
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            current_path = next_path
        return current_fd, parts[-1]
    except BaseException:
        os.close(current_fd)
        raise


def _snapshot(
    parent_fd: int, name: str, target: Path, owner_uid: int
) -> ManagedFileSnapshot | None:
    try:
        item = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error(target, "inspect", str(exc)) from exc
    if not stat.S_ISREG(item.st_mode):
        raise _error(target, "validate", "existing target is not a regular file")
    if item.st_uid != owner_uid:
        raise _error(target, "validate", f"existing target is not owned by uid {owner_uid}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _error(target, "open", str(exc)) from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino):
            raise _error(target, "inspect", "target changed while it was being opened")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
    except OSError as exc:
        raise _error(target, "read", str(exc)) from exc
    finally:
        os.close(fd)
    return ManagedFileSnapshot(
        b"".join(chunks),
        stat.S_IMODE(item.st_mode),
        item.st_dev,
        item.st_ino,
        item.st_mtime_ns,
    )


def inspect_managed_file(
    *,
    trusted_root: Path,
    target: Path,
    owner_uid: int,
) -> ManagedFileSnapshot | None:
    parent_fd, name = _open_parent(
        trusted_root=trusted_root,
        target=target,
        owner_uid=owner_uid,
        create=False,
        directory_mode=0o700,
    )
    if parent_fd < 0:
        return None
    try:
        return _snapshot(parent_fd, name, target, owner_uid)
    finally:
        os.close(parent_fd)


def ensure_managed_directory(
    *,
    trusted_root: Path,
    directory: Path,
    mode: int,
    owner_uid: int,
    intermediate_mode: int | None = None,
) -> None:
    parent_fd, _ = _open_parent(
        trusted_root=trusted_root,
        target=directory / ".ai-directory-boundary",
        owner_uid=owner_uid,
        create=True,
        directory_mode=mode if intermediate_mode is None else intermediate_mode,
    )
    try:
        item = os.fstat(parent_fd)
        _validate_directory(item, directory, owner_uid)
        if stat.S_IMODE(item.st_mode) != mode:
            try:
                os.fchmod(parent_fd, mode)
                os.fsync(parent_fd)
            except OSError as exc:
                raise _error(directory, "set directory mode", str(exc)) from exc
            if stat.S_IMODE(os.fstat(parent_fd).st_mode) != mode:
                raise _error(directory, "verify directory", "mode is not exact")
    finally:
        os.close(parent_fd)


def replace_managed_file(
    *,
    trusted_root: Path,
    target: Path,
    content: bytes | str,
    mode: int,
    owner_uid: int,
    expected: ManagedFileSnapshot | None,
    existing_policy: ExistingFilePolicy,
    directory_mode: int = 0o700,
) -> bool:
    desired = content.encode("utf-8") if isinstance(content, str) else content
    parent_fd, name = _open_parent(
        trusted_root=trusted_root,
        target=target,
        owner_uid=owner_uid,
        create=True,
        directory_mode=directory_mode,
    )
    parent = target.parent
    temp_name: str | None = None
    try:
        current = _snapshot(parent_fd, name, target, owner_uid)
        if current != expected:
            raise _error(target, "replace", "target changed after inspection")
        if (
            current is not None
            and existing_policy is ExistingFilePolicy.EXACT_CONTENT
            and current.content != desired
        ):
            raise _error(
                target,
                "replace",
                "the path exists but is not recognized as managed by ai; "
                "it was left unchanged; inspect the path and resolve the collision",
            )
        if current is not None and current.content == desired and current.mode == mode:
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _ in range(128):
            candidate = f".{name}.ai-{secrets.token_hex(12)}"
            try:
                fd = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise _error(target, "create temporary file", str(exc)) from exc
            temp_name = candidate
            break
        else:
            raise _error(target, "create temporary file", "could not allocate an exclusive name")
        try:
            offset = 0
            while offset < len(desired):
                written = os.write(fd, desired[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fchmod(fd, mode)
            os.fsync(fd)
            temp_stat = os.fstat(fd)
            if temp_stat.st_uid != owner_uid or stat.S_IMODE(temp_stat.st_mode) != mode:
                raise _error(target, "verify temporary file", "ownership or mode is not exact")
        except OSError as exc:
            raise _error(target, "write temporary file", str(exc)) from exc
        finally:
            os.close(fd)
        if _snapshot(parent_fd, name, target, owner_uid) != current:
            raise _error(target, "replace", "target changed before atomic replacement")
        try:
            os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temp_name = None
        except OSError as exc:
            raise _error(target, "atomic replace", str(exc)) from exc
        try:
            fsync_directory(parent_fd, parent)
        except ValidationError as exc:
            final = _snapshot(parent_fd, name, target, owner_uid)
            state = (
                "replacement is visible and verified"
                if final is not None and final.content == desired and final.mode == mode
                else "replacement state could not be verified"
            )
            raise _error(target, "establish durability", f"{exc.reason}; {state}") from exc
        final = _snapshot(parent_fd, name, target, owner_uid)
        if final is None or final.content != desired or final.mode != mode:
            raise _error(
                target, "verify replacement", "final type, ownership, mode, or bytes differ"
            )
        return True
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                fsync_directory(parent_fd, parent)
            except (FileNotFoundError, ValidationError):
                pass
            except OSError:
                pass
        os.close(parent_fd)


def write_workspace_file(path: Path, content: str, mode: int = 0o600) -> None:
    """Write private ephemeral output inside an already scoped temporary workspace."""
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError(f"refusing symbolic workspace output path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
