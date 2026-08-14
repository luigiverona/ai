from __future__ import annotations

import os
import subprocess
from pathlib import Path


class FakeRuntime:
    def __init__(self, home: Path, responses=None, dry_run=False):
        self.home = home
        self.responses = responses or {}
        self.dry_run = dry_run
        self.verbose = False
        self.changes = []
        self.calls = []

    def run(self, argv, *, check=True, mutate=False, cwd=None, **kwargs):
        self.calls.append((tuple(argv), mutate, cwd))
        if mutate and self.dry_run:
            return subprocess.CompletedProcess(argv, 0, "", "")
        answer = self.responses.get(tuple(argv), (0, "", ""))
        if callable(answer):
            answer = answer(argv, cwd)
        result = subprocess.CompletedProcess(argv, *answer)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def sudo(self, argv):
        return self.run(["sudo", "--", *argv], mutate=True)

    def changed(self, text):
        self.changes.append(text)

    def require_command(self, name, component):
        return None

    def command_exists(self, name):
        return True

    def atomic_write(self, path, data, mode=0o600):
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        os.chmod(path, mode)
