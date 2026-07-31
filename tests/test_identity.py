from __future__ import annotations

import importlib.metadata
import tempfile
import tomllib
import unittest
from dataclasses import fields
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

from ai_setup.config.codex import CODEX_PROFILES
from ai_setup.identity import IDENTITY
from ai_setup.version import VersionResolutionError, resolve_version
from tools.identity import IDENTITY as RELEASE_IDENTITY
from tools.identity import project_version


class RuntimeIdentityTests(unittest.TestCase):
    def test_runtime_identity_contract_is_explicit_and_immutable(self) -> None:
        self.assertEqual(
            {field.name: getattr(IDENTITY, field.name) for field in fields(IDENTITY)},
            {
                "display_name": "ai",
                "command_name": "ai",
                "distribution_name": "ai-workstation",
                "import_package": "ai_setup",
                "repository_slug": "luigiverona/ai",
                "installer_endpoint": "https://ai.luigiverona.dev/install",
                "install_root_relative": PurePosixPath(".local/share/ai"),
                "launcher_relative": PurePosixPath(".local/bin/ai"),
                "temporary_prefix": "ai-",
                "ssh_key_filename": "id_ed25519_ai_github",
                "ssh_fragment_filename": "ai-github.conf",
                "shell_managed_marker": "# Added by ai",
                "fish_configuration_filename": "ai.fish",
                "catalog_environment_variable": "AI_WORKSTATION_CATALOG_ROOT",
            },
        )
        with self.assertRaises(AttributeError):
            IDENTITY.command_name = "changed"  # type: ignore[misc]

    def test_all_managed_paths_derive_from_an_injected_home(self) -> None:
        home = Path("/tmp/test-home")
        self.assertEqual(IDENTITY.install_root(home), home / ".local/share/ai")
        self.assertEqual(IDENTITY.launcher(home), home / ".local/bin/ai")
        self.assertEqual(
            IDENTITY.codex_shared_binary(home),
            home / ".local/share/ai/bin/codex",
        )
        self.assertEqual(
            IDENTITY.codex_state_root(home),
            home / ".local/share/ai/codex",
        )
        self.assertEqual(
            [
                IDENTITY.codex_state_root(home) / profile.directory_name
                for profile in CODEX_PROFILES
            ],
            [
                home / ".local/share/ai/codex/01",
                home / ".local/share/ai/codex/02",
            ],
        )
        self.assertEqual(IDENTITY.ssh_key(home), home / ".ssh/id_ed25519_ai_github")
        self.assertEqual(
            IDENTITY.ssh_fragment(home),
            home / ".ssh/config.d/ai-github.conf",
        )
        self.assertEqual(
            IDENTITY.fish_configuration(home),
            home / ".config/fish/conf.d/ai.fish",
        )

    def test_identity_does_not_capture_home_at_import_time(self) -> None:
        first = Path("/tmp/first-home")
        second = Path("/tmp/second-home")
        with patch("pathlib.Path.home", return_value=first):
            first_path = IDENTITY.install_root(Path.home())
        with patch("pathlib.Path.home", return_value=second):
            second_path = IDENTITY.install_root(Path.home())
        self.assertEqual(first_path, first / ".local/share/ai")
        self.assertEqual(second_path, second / ".local/share/ai")
        self.assertNotEqual(first_path, second_path)

    def test_release_identity_matches_explicit_runtime_contract(self) -> None:
        self.assertEqual(RELEASE_IDENTITY.display_name, "ai")
        self.assertEqual(RELEASE_IDENTITY.command_name, "ai")
        self.assertEqual(RELEASE_IDENTITY.distribution_name, "ai-workstation")
        self.assertEqual(RELEASE_IDENTITY.import_package, "ai_setup")
        self.assertEqual(RELEASE_IDENTITY.repository_slug, "luigiverona/ai")
        self.assertEqual(RELEASE_IDENTITY.archive_stem, "ai")
        self.assertEqual(RELEASE_IDENTITY.install_root_relative, ".local/share/ai")
        self.assertEqual(RELEASE_IDENTITY.launcher_relative, ".local/bin/ai")
        self.assertEqual(
            RELEASE_IDENTITY.catalog_environment_variable, "AI_WORKSTATION_CATALOG_ROOT"
        )
        self.assertEqual(
            RELEASE_IDENTITY.release_assets("1.0.1"),
            {
                "install",
                "ai-1.0.1.tar.gz",
                "ai-1.0.1.tar.gz.sha256",
                "SHA256SUMS",
            },
        )
        self.assertEqual(
            (
                IDENTITY.display_name,
                IDENTITY.command_name,
                IDENTITY.distribution_name,
                IDENTITY.import_package,
                IDENTITY.repository_slug,
                IDENTITY.install_root_relative.as_posix(),
                IDENTITY.launcher_relative.as_posix(),
                IDENTITY.catalog_environment_variable,
            ),
            (
                RELEASE_IDENTITY.display_name,
                RELEASE_IDENTITY.command_name,
                RELEASE_IDENTITY.distribution_name,
                RELEASE_IDENTITY.import_package,
                RELEASE_IDENTITY.repository_slug,
                RELEASE_IDENTITY.install_root_relative,
                RELEASE_IDENTITY.launcher_relative,
                RELEASE_IDENTITY.catalog_environment_variable,
            ),
        )


class VersionResolutionTests(unittest.TestCase):
    def write_project(
        self, root: Path, *, name: str = "ai-workstation", version: str = "1.0.1"
    ) -> Path:
        package_file = root / "src/ai_setup/version.py"
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_text("# fixture\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        return package_file

    def test_pyproject_is_the_source_checkout_version(self) -> None:
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["version"], "1.0.1")
        self.assertEqual(project_version(Path.cwd()), "1.0.1")
        self.assertEqual(resolve_version(), "1.0.1")
        self.assertNotIn(
            '__version__ = "1.0.1"',
            Path("src/ai_setup/__init__.py").read_text(encoding="utf-8"),
        )

    def test_extracted_release_layout_uses_its_own_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_file = self.write_project(Path(raw))
            metadata = Mock(return_value="9.9.9")
            self.assertEqual(
                resolve_version(package_file=package_file, metadata_version=metadata),
                "1.0.1",
            )
            metadata.assert_not_called()

    def test_installed_layout_falls_back_to_distribution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_file = Path(raw) / "site-packages/ai_setup/version.py"
            package_file.parent.mkdir(parents=True)
            package_file.write_text("# installed fixture\n", encoding="utf-8")
            metadata = Mock(return_value="1.0.1")
            self.assertEqual(
                resolve_version(package_file=package_file, metadata_version=metadata),
                "1.0.1",
            )
            metadata.assert_called_once_with("ai-workstation")

    def test_wrong_project_name_and_malformed_version_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wrong_name = self.write_project(root, name="other")
            with self.assertRaisesRegex(VersionResolutionError, "project name"):
                resolve_version(package_file=wrong_name)
            malformed = self.write_project(root, version="release")
            with self.assertRaisesRegex(VersionResolutionError, "semantic"):
                resolve_version(package_file=malformed)

    def test_missing_trusted_pyproject_fails_without_metadata_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_file = Path(raw) / "src/ai_setup/version.py"
            package_file.parent.mkdir(parents=True)
            package_file.write_text("# fixture\n", encoding="utf-8")
            metadata = Mock(return_value="1.0.1")
            with self.assertRaisesRegex(VersionResolutionError, "metadata is missing"):
                resolve_version(package_file=package_file, metadata_version=metadata)
            metadata.assert_not_called()

    def test_unrelated_parent_project_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "unrelated"\nversion = "9.9.9"\n',
                encoding="utf-8",
            )
            package_file = root / "site-packages/ai_setup/version.py"
            package_file.parent.mkdir(parents=True)
            package_file.write_text("# installed fixture\n", encoding="utf-8")
            self.assertEqual(
                resolve_version(
                    package_file=package_file,
                    metadata_version=lambda _name: "1.0.1",
                ),
                "1.0.1",
            )

    def test_missing_installed_metadata_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package_file = Path(raw) / "site-packages/ai_setup/version.py"
            package_file.parent.mkdir(parents=True)
            package_file.write_text("# installed fixture\n", encoding="utf-8")

            def missing(_name: str) -> str:
                raise importlib.metadata.PackageNotFoundError

            with self.assertRaisesRegex(VersionResolutionError, "cannot resolve"):
                resolve_version(package_file=package_file, metadata_version=missing)


if __name__ == "__main__":
    unittest.main()
