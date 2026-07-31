from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_setup.catalog.loader import load_catalog
from ai_setup.errors import ValidationError
from tests.helpers import write_manifest

GOOD = '[[package]]\nname="Git"\nidentifier="git"\nsource="pacman"\n'


class CatalogTests(unittest.TestCase):
    def test_yay_bin_is_preferred_artifact_for_logical_yay_dependency(self) -> None:
        dependency = next(package for package in load_catalog().deps if package.name == "yay")
        self.assertEqual(
            (dependency.identifier, dependency.source.value),
            ("yay-bin", "aur"),
        )

    def test_mullvad_vpn_prefers_official_package(self) -> None:
        catalog = load_catalog()
        vpn = next(package for package in catalog.apps if package.name == "Mullvad VPN")
        self.assertEqual(
            (vpn.identifier, vpn.source.value),
            ("mullvad-vpn", "pacman"),
        )
        identifiers = {package.identifier for package in (*catalog.apps, *catalog.deps)}
        self.assertNotIn("mullvad-vpn-bin", identifiers)
        self.assertNotIn("mullvad-vpn-daemon", identifiers)

    def test_load_and_duplicate_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(root, "apps", "development", GOOD)
            write_manifest(root, "deps", "runtime", GOOD)
            catalog = load_catalog(root)
            self.assertEqual(len(catalog.apps), 1)
            self.assertEqual(len(catalog.deps), 1)

    def test_unsafe_identifier_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(
                root,
                "apps",
                "development",
                GOOD.replace('identifier="git"', 'identifier="git;bad"'),
            )
            write_manifest(root, "deps", "runtime", GOOD)
            with self.assertRaises(ValidationError):
                load_catalog(root)

    def test_unknown_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(root, "apps", "development", GOOD + 'surprise="x"\n')
            write_manifest(root, "deps", "runtime", GOOD)
            with self.assertRaises(ValidationError):
                load_catalog(root)

    def test_removed_executable_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(root, "apps", "development", GOOD + 'executable="git"\n')
            write_manifest(root, "deps", "runtime", GOOD)
            with self.assertRaises(ValidationError):
                load_catalog(root)
