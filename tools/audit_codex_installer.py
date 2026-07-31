#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import cast
from urllib.parse import urljoin, urlparse

if __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_setup.config.codex_installer import (  # type: ignore[import-not-found]
    TRUSTED_CODEX_INSTALLER,
    verify_codex_installer,
)


class AuditedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urljoin(req.full_url, newurl)
        parsed = urlparse(target)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in TRUSTED_CODEX_INSTALLER.approved_hosts
        ):
            raise ValueError(f"unapproved redirect target: {parsed.scheme}://{parsed.hostname}")
        return super().redirect_request(req, fp, code, msg, headers, target)


def download(url: str) -> tuple[bytes, str, str]:
    opener = urllib.request.build_opener(AuditedRedirectHandler())
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_CODEX_INSTALLER.approved_hosts:
        raise ValueError("audit URL is not an approved HTTPS endpoint")
    request = urllib.request.Request(  # noqa: S310 - scheme and host validated above
        url, headers={"User-Agent": "ai-codex-installer-audit/1"}
    )
    with opener.open(request, timeout=30) as response:
        content = response.read(TRUSTED_CODEX_INSTALLER.maximum_bytes + 1)
        return content, response.geturl(), response.headers.get_content_type()


def audit(served: bytes, upstream: bytes, *, effective_url: str, content_type: str) -> str:
    """Verify provenance without writing trust data or executing installer content."""
    digest = verify_codex_installer(
        served,
        effective_url=effective_url,
        content_type=content_type,
        reported_size=len(served),
        provenance=TRUSTED_CODEX_INSTALLER,
    )
    if served != upstream:
        raise ValueError("served installer differs from the audited upstream commit")
    return cast(str, digest)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit the pinned Codex installer without executing it"
    )
    parser.add_argument("--served-file", type=Path)
    parser.add_argument("--upstream-file", type=Path)
    args = parser.parse_args()
    if args.served_file:
        served = args.served_file.read_bytes()
        effective_url = "https://releases.openai.com/codex/install.sh"
        content_type = "text/x-sh"
    else:
        served, effective_url, content_type = download(TRUSTED_CODEX_INSTALLER.canonical_url)
    if args.upstream_file:
        upstream = args.upstream_file.read_bytes()
    else:
        raw_url = (
            "https://raw.githubusercontent.com/openai/codex/"
            f"{TRUSTED_CODEX_INSTALLER.upstream_commit}/"
            f"{TRUSTED_CODEX_INSTALLER.upstream_path}"
        )
        with urllib.request.urlopen(raw_url, timeout=30) as response:  # noqa: S310 - fixed HTTPS origin
            upstream = response.read(TRUSTED_CODEX_INSTALLER.maximum_bytes + 1)
    digest = audit(served, upstream, effective_url=effective_url, content_type=content_type)
    print(f"audit: PASS\nsha256: {digest}\nupstream: {TRUSTED_CODEX_INSTALLER.upstream_commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
