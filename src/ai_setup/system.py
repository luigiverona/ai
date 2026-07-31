from __future__ import annotations

import os
import platform
import shutil
import socket
from pathlib import Path

from ai_setup.errors import ValidationError
from ai_setup.identity import IDENTITY


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
    if "ID=arch" not in release:
        raise ValidationError("system", "validate operating system", "Arch Linux is required")
    if platform.machine() != "x86_64":
        raise ValidationError("system", "validate architecture", "x86_64 is required")
    for command in ("pacman", "sudo", "curl", "mktemp", "sha256sum", "tar"):
        if not shutil.which(command):
            raise ValidationError("system", "validate capabilities", f"{command} is unavailable")
    if require_network:
        try:
            socket.getaddrinfo("archlinux.org", 443)
        except OSError as exc:
            raise ValidationError("network", "resolve archlinux.org", str(exc)) from exc
