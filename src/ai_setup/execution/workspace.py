from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ai_setup.identity import IDENTITY


class TemporaryWorkspace:
    CHILDREN = ("aur", "downloads", "logs", "state")

    def __init__(self, *, keep: bool = False, temp_root: Path | None = None) -> None:
        self.keep = keep
        self.temp_root = temp_root or Path(os.environ.get("TMPDIR", "/tmp"))
        self.path: Path | None = None
        self.failed = False

    def __enter__(self) -> TemporaryWorkspace:
        self.path = Path(tempfile.mkdtemp(prefix=IDENTITY.temporary_prefix, dir=self.temp_root))
        self.path.chmod(0o700)
        for child in self.CHILDREN:
            (self.path / child).mkdir(mode=0o700)
        return self

    @staticmethod
    def safe_cleanup(path: Path, temp_root: Path) -> None:
        resolved = path.resolve()
        root = temp_root.resolve()
        if (
            resolved == root
            or root not in resolved.parents
            or not resolved.name.startswith(IDENTITY.temporary_prefix)
        ):
            raise ValueError("refusing unsafe workspace cleanup")
        shutil.rmtree(resolved)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc is not None:
            self.failed = True
        if self.path and not self.keep and not self.failed:
            self.safe_cleanup(self.path, self.temp_root)
