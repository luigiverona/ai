#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.identity import IDENTITY

VERSION_TOKEN = IDENTITY.version_token  # noqa: S105 - deterministic template marker
DIGEST_TOKEN = IDENTITY.digest_token  # noqa: S105 - deterministic template marker
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
UNRESOLVED_PATTERN = re.compile(r"@[A-Z0-9_]+@")
GITHUB_RELEASE_BASE = (
    f'readonly RELEASE_BASE="${{AI_WORKSTATION_RELEASE_BASE:-{IDENTITY.release_base_url}}}"'
)
INSTALLER_SECURITY_CONTRACT = (
    'readonly ROOT_MARKER_NAME=".ai-workstation-installation"',
    'readonly ROOT_MARKER_CONTENT="ai-workstation installation format 1"',
    'readonly LAUNCHER_MARKER="# ai-workstation managed launcher format 1"',
    'readonly FISH_MARKER="# ai-workstation managed fish PATH format 1"',
    'readonly PATH_BLOCK_BEGIN="# >>> ai-workstation managed PATH >>>"',
    'readonly PATH_BLOCK_END="# <<< ai-workstation managed PATH <<<"',
    'readonly JOURNAL_NAME=".ai-workstation-transaction.json"',
    'readonly JOURNAL_SCHEMA="1"',
)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_template(content: str) -> None:
    for token, label in ((VERSION_TOKEN, "version"), (DIGEST_TOKEN, "checksum")):
        count = content.count(token)
        if count != 1:
            raise ValueError(
                f"installer {label} placeholder must occur exactly once, found {count}"
            )
    unresolved = set(UNRESOLVED_PATTERN.findall(content)) - {VERSION_TOKEN, DIGEST_TOKEN}
    if unresolved:
        raise ValueError(f"installer template has unresolved placeholders: {sorted(unresolved)}")


def validate_installer(content: str, version: str, archive_digest: str) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("installer version must be semantic X.Y.Z")
    if not DIGEST_PATTERN.fullmatch(archive_digest):
        raise ValueError("installer archive digest must be 64 lowercase hexadecimal characters")
    if UNRESOLVED_PATTERN.search(content):
        raise ValueError("generated installer contains an unresolved placeholder")
    version_line = f'readonly AI_WORKSTATION_VERSION="{version}"'
    digest_line = f'readonly EXPECTED_SHA256="{archive_digest}"'
    if content.count(version_line) != 1:
        raise ValueError("generated installer must contain exactly one version declaration")
    if content.count(digest_line) != 1:
        raise ValueError("generated installer must contain exactly one checksum declaration")
    if content.count(version) != 1:
        raise ValueError("generated installer must embed the version exactly once")
    if content.count(archive_digest) != 1:
        raise ValueError("generated installer must embed the checksum exactly once")
    if "AI_WORKSTATION_RELEASE_SHA256" in content:
        raise ValueError("generated installer must not permit a checksum override")
    if re.search(r"curl[^\n]*\.sha256", content):
        raise ValueError("generated installer must not download a checksum")
    if content.count(GITHUB_RELEASE_BASE) != 1:
        raise ValueError("generated installer must contain exactly one GitHub Release base")
    missing_contract = [value for value in INSTALLER_SECURITY_CONTRACT if content.count(value) != 1]
    if missing_contract:
        raise ValueError(f"generated installer security contract differs: {missing_contract}")
    if content.count("PYTHONDONTWRITEBYTECODE=1") != 2:
        raise ValueError("generated installer must disable bytecode in validation and launch")
    forbidden = (
        "https://ai.luigiverona.dev",
        "/releases/v",
        "latest/download",
        "releases/latest",
        "api.github.com",
    )
    if any(value in content for value in forbidden):
        raise ValueError("generated installer contains a forbidden release source")


def render_installer(template: str, version: str, archive_digest: str) -> str:
    validate_template(template)
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("installer version must be semantic X.Y.Z")
    if not DIGEST_PATTERN.fullmatch(archive_digest):
        raise ValueError("installer archive digest must be 64 lowercase hexadecimal characters")
    rendered = template.replace(VERSION_TOKEN, version).replace(DIGEST_TOKEN, archive_digest)
    validate_installer(rendered, version, archive_digest)
    return rendered


def atomic_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755)  # noqa: S103 - published installer is executable
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_installer(template: Path, version: str, archive: Path, output: Path) -> str:
    expected_name = IDENTITY.archive_name(version)
    if archive.name != expected_name:
        raise ValueError(f"installer archive must be named {expected_name}")
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("installer archive must be a regular file")
    digest = file_digest(archive)
    rendered = render_installer(template.read_text(encoding="utf-8"), version, digest)
    atomic_executable(output, rendered)
    return file_digest(output)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Build an immutable {IDENTITY.command_name} bootstrap installer"
    )
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        digest = build_installer(
            args.template.resolve(),
            args.version,
            args.archive.resolve(),
            args.output.resolve(),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"installer build failed: {exc}") from exc
    print(f"{digest}  {args.output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
