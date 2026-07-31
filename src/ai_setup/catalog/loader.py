from __future__ import annotations

import re
import tomllib
from pathlib import Path

from ai_setup.errors import ValidationError
from ai_setup.models import Catalog, Package, PackageKind, Source

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@:-]*$")
KEYS = {"name", "identifier", "source"}


def default_catalog_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "apps").is_dir() and (parent / "deps").is_dir():
            return parent
    return Path.cwd()


def _read_kind(root: Path, kind: str) -> tuple[tuple[Package, ...], frozenset[str]]:
    packages: list[Package] = []
    categories: set[str] = set()
    base = root / kind
    if not base.is_dir():
        raise ValidationError("catalog", "load", f"missing {base}")
    for path in sorted(base.glob("*/manifest.toml")):
        category = path.parent.name
        categories.add(category)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError("catalog", "parse", f"{path}: {exc}") from exc
        entries = data.get("package")
        if not isinstance(entries, list):
            raise ValidationError("catalog", "validate", f"{path}: package must be an array")
        for raw in entries:
            if not isinstance(raw, dict) or not {"name", "identifier", "source"} <= raw.keys():
                raise ValidationError("catalog", "validate", f"{path}: invalid package entry")
            unknown = set(raw) - KEYS
            if unknown:
                raise ValidationError(
                    "catalog", "validate", f"{path}: unknown keys {sorted(unknown)}"
                )
            identifier = raw["identifier"]
            if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
                raise ValidationError("catalog", "validate", f"{path}: unsafe identifier")
            try:
                source = Source(raw["source"])
            except (ValueError, TypeError) as exc:
                raise ValidationError("catalog", "validate", f"{path}: unknown source") from exc
            packages.append(
                Package(
                    source,
                    identifier,
                    str(raw["name"]),
                    category,
                    PackageKind.APPLICATION if kind == "apps" else PackageKind.DEPENDENCY,
                )
            )
    return tuple(packages), frozenset(categories)


def load_catalog(root: Path | None = None) -> Catalog:
    root = root or default_catalog_root()
    apps, app_categories = _read_kind(root, "apps")
    deps, _ = _read_kind(root, "deps")
    seen: dict[tuple[Source, str], Package] = {}
    for package in (*apps, *deps):
        key = (package.source, package.identifier)
        previous = seen.get(key)
        if previous and previous.name != package.name:
            raise ValidationError(
                "catalog", "validate", f"conflicting duplicate {package.identifier}"
            )
        seen[key] = package
    return Catalog(apps, deps, app_categories)
