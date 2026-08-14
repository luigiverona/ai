import os
import platform
import stat
from pathlib import Path

from ..errors import AiError
from ..runtime import Runtime


def reconcile(runtime: Runtime) -> None:
    if platform.system() != "Linux":
        raise AiError("Precheck: Linux is required")
    release = Path("/etc/os-release").read_text()
    values = dict(line.split("=", 1) for line in release.splitlines() if "=" in line)
    if values.get("ID", "").strip('"') != "arch":
        raise AiError("Precheck: Arch Linux is required")
    if platform.machine() != "x86_64":
        raise AiError("Precheck: x86-64 is required")
    if os.geteuid() == 0:
        raise AiError("Precheck: run ai as a normal non-root user")
    if not runtime.home.is_absolute() or not runtime.home.exists():
        raise AiError("Precheck: invalid home directory")
    info = runtime.home.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AiError("Precheck: home directory must be a user-owned real directory")
