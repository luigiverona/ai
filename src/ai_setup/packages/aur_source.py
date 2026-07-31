from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ai_setup.errors import ValidationError


@dataclass(frozen=True, slots=True)
class AurSource:
    repository: str
    commit: str
    package_base: str
    package_names: tuple[str, ...]
    tree_sha256: str


YAY_BIN_SOURCE = AurSource(
    repository="https://aur.archlinux.org/yay-bin.git",
    commit="13e0a4754d106a9252b7479bf1b370fbe454fc48",
    package_base="yay-bin",
    package_names=("yay-bin",),
    tree_sha256="a98e7e25d3c0b3a2ee92ba09bf72f44ee321922b02b2300caae6dac982583fe4",
)


def tracked_tree_sha256(root: Path, index: str, status: str) -> str:
    """Hash the exact clean tracked tree described by `git ls-files --stage`."""
    if status:
        raise ValidationError("aur", "validate yay", "AUR checkout is not clean")

    entries: list[tuple[bytes, str, Path]] = []
    seen: set[bytes] = set()
    for record in index.split("\0"):
        if not record:
            continue
        metadata, separator, path_text = record.partition("\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise ValidationError("aur", "validate yay", "invalid AUR Git index")
        mode, object_id, stage = parts
        if (
            not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
            or stage != "0"
            or mode not in {"100644", "100755"}
        ):
            raise ValidationError("aur", "validate yay", "unsupported AUR tracked entry")
        try:
            path_bytes = path_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValidationError("aur", "validate yay", "invalid AUR tracked path") from exc
        if (
            not path_text
            or path_text.startswith("/")
            or "\0" in path_text
            or ".." in Path(path_text).parts
            or path_bytes in seen
        ):
            raise ValidationError("aur", "validate yay", "unsafe AUR tracked path")
        seen.add(path_bytes)
        entries.append((path_bytes, mode, root / path_text))

    if not entries:
        raise ValidationError("aur", "validate yay", "AUR checkout has no tracked files")

    expected_files = {path.relative_to(root) for _, _, path in entries}
    expected_directories = {
        parent for path in expected_files for parent in path.parents if parent != Path(".")
    }
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise ValidationError("aur", "validate yay", "unsupported AUR tracked entry")
        if path.is_dir():
            if relative not in expected_directories:
                raise ValidationError("aur", "validate yay", "unexpected AUR directory")
        elif relative not in expected_files:
            raise ValidationError("aur", "validate yay", "unexpected AUR file")

    digest = hashlib.sha256()
    for path_bytes, mode, path in sorted(entries, key=lambda entry: entry[0]):
        if path.is_symlink() or not path.is_file():
            raise ValidationError("aur", "validate yay", "unsupported AUR tracked entry")
        stat = path.stat()
        expected_mode = 0o755 if mode == "100755" else 0o644
        if stat.st_mode & 0o777 != expected_mode or stat.st_nlink != 1:
            raise ValidationError("aur", "validate yay", "unexpected AUR tracked file metadata")
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii")
        digest.update(mode.encode("ascii"))
        digest.update(b"\0blob\0")
        digest.update(path_bytes)
        digest.update(b"\0")
        digest.update(file_digest)
        digest.update(b"\n")
    return digest.hexdigest()


def srcinfo_identity(srcinfo: str) -> tuple[str | None, tuple[str, ...]]:
    package_base: str | None = None
    package_names: list[str] = []
    for line in srcinfo.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if not separator:
            continue
        if key == "pkgbase":
            package_base = value
        elif key == "pkgname":
            package_names.append(value)
    return package_base, tuple(package_names)
