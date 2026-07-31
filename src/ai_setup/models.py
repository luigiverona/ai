from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Source(StrEnum):
    PACMAN = "pacman"
    AUR = "aur"
    FLATPAK = "flatpak"
    UPSTREAM = "upstream"


class PackageKind(StrEnum):
    APPLICATION = "application"
    DEPENDENCY = "dependency"


class Capability(StrEnum):
    SYSTEM = "system"
    DEPS = "deps"
    APPS = "apps"
    FLATPAK = "flatpak"
    FLATHUB = "flathub"
    GIT = "git"
    GITHUB = "github"
    SSH = "ssh"
    CODEX = "codex"
    SHELL = "shell"


@dataclass(frozen=True, slots=True, order=True)
class Package:
    source: Source
    identifier: str
    name: str
    category: str
    kind: PackageKind = PackageKind.APPLICATION


@dataclass(frozen=True, slots=True)
class Catalog:
    apps: tuple[Package, ...]
    deps: tuple[Package, ...]
    app_categories: frozenset[str]


@dataclass(frozen=True, slots=True)
class Selection:
    capabilities: frozenset[Capability]
    app_categories: frozenset[str] = frozenset()
    complete: bool = False


@dataclass(frozen=True, slots=True)
class Plan:
    selected: tuple[Capability, ...]
    prerequisites: tuple[Capability, ...]
    packages: tuple[Package, ...]


@dataclass(slots=True)
class RunOptions:
    dry_run: bool = False
    assume_yes: bool = False
    verbose: bool = False
    keep_temp: bool = False
    home: Path = field(default_factory=Path.home)


class WorkflowStage(StrEnum):
    ADMINISTRATOR = "Administrator access"
    SYSTEM = "System update"
    APPLICATIONS = "Applications"
    FLATPAK = "Flatpak"
    GIT = "Git"
    GITHUB = "GitHub"
    SSH = "SSH"
    CODEX = "Codex"
    SHELL = "Shell PATH"
    VERIFICATION = "Verification"


@dataclass(frozen=True, slots=True)
class StageSpec:
    stage: WorkflowStage
    capabilities: frozenset[Capability]
    resume_label: str
    interruption_label: str
    include_when_native_packages_pending: bool = False
    always: bool = False

    @property
    def plan_label(self) -> str:
        return self.stage.value

    def resume_sentence(self, *, first: bool) -> str:
        if not first:
            return self.resume_label
        return self.resume_label[0].upper() + self.resume_label[1:]

    def selected(self, capabilities: set[Capability], *, native_packages_pending: bool) -> bool:
        return (
            self.always
            or bool(capabilities & self.capabilities)
            or (self.include_when_native_packages_pending and native_packages_pending)
        )


STAGE_SPECS = (
    StageSpec(
        WorkflowStage.ADMINISTRATOR,
        frozenset({Capability.SYSTEM}),
        "administrator access",
        "administrator access",
        include_when_native_packages_pending=True,
    ),
    StageSpec(
        WorkflowStage.SYSTEM,
        frozenset({Capability.SYSTEM}),
        "system update",
        "system update",
    ),
    StageSpec(
        WorkflowStage.APPLICATIONS,
        frozenset({Capability.APPS}),
        "applications",
        "application installation",
        include_when_native_packages_pending=True,
    ),
    StageSpec(
        WorkflowStage.FLATPAK,
        frozenset({Capability.FLATPAK, Capability.FLATHUB}),
        "Flatpak",
        "Flatpak configuration",
    ),
    StageSpec(
        WorkflowStage.GIT,
        frozenset({Capability.GIT}),
        "Git",
        "Git configuration",
    ),
    StageSpec(
        WorkflowStage.GITHUB,
        frozenset({Capability.GITHUB}),
        "GitHub",
        "GitHub configuration",
    ),
    StageSpec(
        WorkflowStage.SSH,
        frozenset({Capability.SSH}),
        "SSH",
        "SSH configuration",
    ),
    StageSpec(
        WorkflowStage.CODEX,
        frozenset({Capability.CODEX}),
        "Codex",
        "Codex configuration",
    ),
    StageSpec(
        WorkflowStage.SHELL,
        frozenset({Capability.SHELL}),
        "shell PATH",
        "shell PATH configuration",
    ),
    StageSpec(
        WorkflowStage.VERIFICATION,
        frozenset(),
        "verification",
        "verification",
        always=True,
    ),
)

STAGE_ORDER = tuple(spec.stage for spec in STAGE_SPECS)


def stage_spec(stage: WorkflowStage) -> StageSpec:
    return next(spec for spec in STAGE_SPECS if spec.stage is stage)


@dataclass(slots=True)
class WorkflowProgress:
    selected: tuple[WorkflowStage, ...]
    current: WorkflowStage | None = None
    completed: list[WorkflowStage] = field(default_factory=list)
    mutation_started: bool = False

    @property
    def remaining(self) -> tuple[WorkflowStage, ...]:
        return tuple(stage for stage in self.selected if stage not in self.completed)

    def begin(self, stage: WorkflowStage) -> None:
        self.current = stage

    def finish(self, stage: WorkflowStage) -> None:
        if stage not in self.completed:
            self.completed.append(stage)
        self.current = None
