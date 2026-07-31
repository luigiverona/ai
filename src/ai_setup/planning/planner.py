from __future__ import annotations

from ai_setup.models import Capability, Catalog, Package, Plan, Selection, Source

ORDER = tuple(Capability)
PREREQUISITES: dict[Capability, set[Capability]] = {
    Capability.APPS: {Capability.DEPS},
    Capability.FLATHUB: {Capability.FLATPAK},
    Capability.GITHUB: {Capability.DEPS, Capability.GIT},
    Capability.SSH: {Capability.GITHUB, Capability.GIT},
    Capability.CODEX: {Capability.DEPS, Capability.SHELL},
}


def _closure(selected: set[Capability]) -> set[Capability]:
    result = set(selected)
    changed = True
    while changed:
        before = len(result)
        for capability in tuple(result):
            result.update(PREREQUISITES.get(capability, set()))
        changed = len(result) != before
    return result


def _dedupe(packages: list[Package]) -> tuple[Package, ...]:
    unique = {(p.source, p.identifier): p for p in packages}
    return tuple(sorted(unique.values(), key=lambda p: (p.source.value, p.identifier)))


def build_plan(selection: Selection, catalog: Catalog) -> Plan:
    requested = set(selection.capabilities)
    resolved = _closure(requested)
    selected_app_packages: tuple[Package, ...] = ()
    if Capability.APPS in resolved:
        selected_app_packages = catalog.apps
        if selection.app_categories:
            selected_app_packages = tuple(
                p for p in selected_app_packages if p.category in selection.app_categories
            )
        if any(p.source is Source.FLATPAK for p in selected_app_packages):
            resolved.update({Capability.FLATPAK, Capability.FLATHUB})
        if any(
            p.source is Source.UPSTREAM and p.identifier == "codex" for p in selected_app_packages
        ):
            resolved.add(Capability.CODEX)
        resolved = _closure(resolved)
    packages: list[Package] = []
    if Capability.DEPS in resolved:
        if selection.complete:
            packages.extend(catalog.deps)
        else:
            required_ids: set[str] = set()
            sources = {p.source for p in selected_app_packages}
            if Source.AUR in sources:
                required_ids.update({"git", "base-devel", "yay-bin"})
            if Capability.GIT in resolved:
                required_ids.add("git")
            if Capability.GITHUB in resolved:
                required_ids.update({"git", "github-cli", "openssh"})
            if Capability.SSH in resolved:
                required_ids.add("openssh")
            if Capability.CODEX in resolved:
                required_ids.add("curl")
            packages.extend(p for p in catalog.deps if p.identifier in required_ids)
    if Capability.APPS in resolved:
        packages.extend(selected_app_packages)
    if Capability.FLATPAK in resolved:
        packages.extend(
            p for p in catalog.deps if p.source is Source.PACMAN and p.identifier == "flatpak"
        )
    if Capability.GITHUB in resolved:
        packages.extend(p for p in catalog.deps if p.identifier == "github-cli")
    if Capability.CODEX in resolved:
        packages.extend(
            p for p in catalog.apps if p.source is Source.UPSTREAM and p.identifier == "codex"
        )
    return Plan(
        tuple(c for c in ORDER if c in requested),
        tuple(c for c in ORDER if c in resolved - requested),
        _dedupe(packages),
    )
