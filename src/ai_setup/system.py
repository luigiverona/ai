from __future__ import annotations

import os
import platform
import re
import shutil
import socket
from pathlib import Path

from ai_setup.errors import ValidationError
from ai_setup.identity import IDENTITY

ARCH_REQUIRED = "Arch Linux x86-64 is required."


def is_arch_linux_x86_64(release: str) -> bool:
    exact_arch = any(
        re.fullmatch(r"ID=(?:arch|['\"]arch['\"])", line) for line in release.splitlines()
    )
    return platform.system() == "Linux" and platform.machine() == "x86_64" and exact_arch


def validate_system(*, require_network: bool = True) -> None:
    if os.geteuid() == 0:
        raise ValidationError(
            "system",
            "validate",
            f"run {IDENTITY.command_name} as a normal user, not root",
        )
    try:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError("system", "detect operating system", str(exc)) from exc
    if not is_arch_linux_x86_64(release):
        raise ValidationError("system", "validate operating system", ARCH_REQUIRED)
    for command in ("pacman", "sudo", "curl", "mktemp", "sha256sum", "tar"):
        if not shutil.which(command):
            raise ValidationError("system", "validate capabilities", f"{command} is unavailable")
    if require_network:
        try:
            socket.getaddrinfo("archlinux.org", 443)
        except OSError as exc:
            raise ValidationError("network", "resolve archlinux.org", str(exc)) from exc
