from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ApplicationError(Exception):
    component: str
    operation: str
    reason: str
    exit_code: int = 1
    log_path: str | None = None
    packages: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.component}: {self.operation} failed: {self.reason}"


class ValidationError(ApplicationError):
    pass


class CommandError(ApplicationError):
    pass
