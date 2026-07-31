from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

from ai_setup.errors import ValidationError


@dataclass(frozen=True, slots=True)
class CodexInstallerProvenance:
    canonical_url: str
    approved_hosts: tuple[str, ...]
    upstream_repository: str
    upstream_commit: str
    upstream_path: str
    audited_on: str
    sha256: str
    maximum_bytes: int


TRUSTED_CODEX_INSTALLER = CodexInstallerProvenance(
    canonical_url="https://chatgpt.com/codex/install.sh",
    approved_hosts=("chatgpt.com", "releases.openai.com"),
    upstream_repository="https://github.com/openai/codex",
    upstream_commit="39a2438d16514d0d6f88105d17b0f747994af487",
    upstream_path="scripts/install/install.sh",
    audited_on="2026-07-31",
    sha256="ba92dd27e5c06f0d3bbc58bfa4b9cfb6599cd2742fbb1f92a2765e6c07dedb5a",
    maximum_bytes=131_072,
)


def verify_codex_installer(
    content: bytes,
    *,
    effective_url: str,
    content_type: str,
    reported_size: int,
    provenance: CodexInstallerProvenance = TRUSTED_CODEX_INSTALLER,
    verbose: bool = False,
) -> str:
    parsed = urlparse(effective_url)
    if parsed.scheme != "https" or parsed.hostname not in provenance.approved_hosts:
        raise ValidationError("codex", "verify official installer", "unapproved installer host")
    if reported_size != len(content) or len(content) > provenance.maximum_bytes:
        raise ValidationError("codex", "verify official installer", "invalid installer size")
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type not in {"text/x-sh", "text/plain", "application/x-sh"}:
        raise ValidationError("codex", "verify official installer", "invalid installer media type")
    if not content.startswith(b"#!/bin/sh\n"):
        raise ValidationError("codex", "verify official installer", "invalid installer interpreter")
    digest = hashlib.sha256(content).hexdigest()
    if digest != provenance.sha256:
        detail = ""
        if verbose:
            detail = (
                f"; expected SHA-256 {provenance.sha256}; actual SHA-256 {digest}; "
                f"source {provenance.canonical_url}; audited upstream commit "
                f"{provenance.upstream_commit}"
            )
        raise ValidationError(
            "codex",
            "verify official installer",
            "installer differs from the audited version; no installer was executed; "
            f"ai requires a reviewed release before continuing{detail}",
        )
    return digest
