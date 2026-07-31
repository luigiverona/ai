from __future__ import annotations

import ast
import importlib
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ai_setup
from ai_setup.catalog.loader import load_catalog
from ai_setup.config.codex import CodexManager
from ai_setup.execution.runner import CommandRunner
from ai_setup.models import Capability, Plan, RunOptions
from ai_setup.status import StatusWorkflow
from ai_setup.ui.terminal import Terminal
from ai_setup.verification import readiness
from ai_setup.verification.checks import CheckResult, Verifier
from ai_setup.workflow import Workflow
from tests.helpers import FakeRunner

ROOT = Path(__file__).resolve().parents[1]
REEXPORT_PACKAGES = ("catalog", "execution", "packages", "planning", "ui")
FORMER_EXPORTS = {
    "catalog": {"default_catalog_root", "load_catalog"},
    "execution": {
        "Command",
        "CommandResult",
        "CommandRunner",
        "TemporaryWorkspace",
        "interactive_terminal_available",
        "manual_authentication_env",
    },
    "packages": {"AurManager", "FlatpakManager", "PacmanManager", "YayReadiness", "yay_readiness"},
    "planning": {"StateInspector", "build_plan"},
    "ui": {"Terminal"},
}


class PublicContractTests(unittest.TestCase):
    def test_exactly_one_console_entry_point_and_module_execution_work(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"], {"ai": "ai_setup.cli:main"})
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "ai_setup", "--version"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "ai 2.0.0\n", ""))
        self.assertEqual(project["project"]["name"], "ai-workstation")
        self.assertTrue((ROOT / "src/ai_setup").is_dir())
        self.assertFalse((ROOT / "src/ai").exists())

    def test_clean_break_identity_has_no_compatibility_surface(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], "2.0.0")
        self.assertEqual(
            project["project"]["urls"]["Repository"], "https://github.com/luigiverona/ai"
        )
        self.assertEqual(
            project["project"]["scripts"],
            {"ai": "ai_setup.cli:main"},
        )
        self.assertFalse((ROOT / "src" / ("om" + "fg")).exists())
        self.assertEqual(
            sorted(path.name for path in (ROOT / "docs/releases").glob("*.md")),
            ["v1.0.0.md", "v1.0.1.md", "v1.0.2.md", "v1.0.3.md", "v2.0.0.md"],
        )

        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        forbidden_identity = ("om" + "fg").casefold()
        self.assertFalse(
            [
                path
                for path in tracked
                if (ROOT / path).is_file()
                if forbidden_identity in path.casefold()
                or forbidden_identity
                in (ROOT / path).read_text(encoding="utf-8", errors="ignore").casefold()
            ]
        )

    def test_typed_library_marker_and_package_data_are_absent(self) -> None:
        self.assertFalse((ROOT / "src/ai_setup/py.typed").exists())
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        setuptools = project.get("tool", {}).get("setuptools", {})
        self.assertNotIn("package-data", setuptools)

    def test_top_level_and_subpackage_initializers_expose_no_implementation_api(self) -> None:
        self.assertIsNone(ai_setup.__dict__.get("__all__"))
        for name, former in FORMER_EXPORTS.items():
            with self.subTest(package=name):
                package = importlib.import_module(f"ai_setup.{name}")
                self.assertTrue(former.isdisjoint(vars(package)))
                tree = ast.parse(
                    (ROOT / f"src/ai_setup/{name}/__init__.py").read_text(encoding="utf-8")
                )
                self.assertFalse(
                    any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in tree.body)
                )

    def test_production_and_tests_use_concrete_implementation_imports(self) -> None:
        forbidden = {f"ai_setup.{name}" for name in REEXPORT_PACKAGES}
        for base in (ROOT / "src", ROOT / "tests", ROOT / "tools"):
            for path in sorted(base.rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn(
                            node.module,
                            forbidden,
                            f"{path.relative_to(ROOT)}:{node.lineno}",
                        )

    def test_all_importable_production_modules_load_without_runtime_actions(self) -> None:
        script = """
import importlib
import pkgutil
from pathlib import Path
from unittest.mock import patch
import ai_setup

modules = sorted(
    module.name
    for module in pkgutil.walk_packages(ai_setup.__path__, prefix="ai_setup.")
    if module.name != "ai_setup.__main__"
)
with (
    patch("subprocess.run", side_effect=AssertionError("import executed subprocess")),
    patch("pathlib.Path.home", side_effect=AssertionError("import captured home")),
):
    for name in modules:
        importlib.import_module(name)
print(len(modules))
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(int(result.stdout), 0)


class UnscopedCodexPreservationTests(unittest.TestCase):
    def assert_complete_readiness_ignores_unscoped_path(self, home: Path) -> None:
        catalog = load_catalog()
        scope = readiness.ReadinessScope.complete(catalog)
        package_results = [CheckResult(package.name, True) for package in scope.packages]
        with (
            patch.object(Verifier, "system", return_value=CheckResult("supported system", True)),
            patch.object(Verifier, "package", side_effect=package_results),
            patch.object(Verifier, "flathub", return_value=CheckResult("Flathub remote", True)),
            patch("ai_setup.verification.readiness.GitConfigurator.verify", return_value=True),
            patch(
                "ai_setup.verification.readiness.GitHubConfigurator.authenticated",
                return_value=True,
            ),
            patch(
                "ai_setup.verification.readiness.GitHubConfigurator.protocol", return_value="ssh"
            ),
            patch("ai_setup.verification.readiness.SSHManager.verify", return_value=True),
            patch("ai_setup.verification.readiness.CodexManager.verified", return_value=True),
            patch(
                "ai_setup.verification.readiness.CodexManager.profiles_distinct", return_value=True
            ),
            patch.object(
                Verifier,
                "shell_configuration",
                return_value=CheckResult("shell PATH configuration", True),
            ),
        ):
            results = readiness.ReadinessVerifier(
                scope,
                CommandRunner(),
                home,
                target_shell=Mock(),
            ).results(read_only=True)
        self.assertTrue(all(result.passed for result in results))
        self.assertNotIn("unscoped Codex launcher", [result.name for result in results])

    def test_unrelated_regular_executable_is_preserved_by_profiles_readiness_and_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            path = home / ".local/bin/codex"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"#!/bin/sh\nprintf unrelated\n")
            path.chmod(0o751)
            before = (path.read_bytes(), path.stat().st_mode, path.stat().st_ino)

            with patch("ai_setup.workflow.validate_system"):
                dry_run_code = Workflow(
                    Plan((Capability.CODEX,), (), ()),
                    RunOptions(dry_run=True, home=home),
                    Terminal(output=lambda _line: None),
                    runner=FakeRunner(dry_run=True),  # type: ignore[arg-type]
                ).run()
            self.assertEqual(dry_run_code, 0)
            CodexManager(FakeRunner(), home).create_profiles()  # type: ignore[arg-type]
            self.assert_complete_readiness_ignores_unscoped_path(home)
            lines: list[str] = []
            with (
                patch.object(
                    readiness.ReadinessVerifier,
                    "results",
                    return_value=[CheckResult("supported system", True)],
                ),
                patch("ai_setup.verification.readiness.render_readiness"),
            ):
                code = StatusWorkflow(
                    load_catalog(),
                    RunOptions(home=home),
                    Terminal(output=lines.append),
                ).run()
            self.assertEqual(code, 0)
            self.assertEqual((path.read_bytes(), path.stat().st_mode, path.stat().st_ino), before)

    def test_unrelated_symlink_and_target_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            target = home / "outside-managed-root"
            target.write_bytes(b"user content")
            path = home / ".local/bin/codex"
            path.parent.mkdir(parents=True)
            path.symlink_to(target)
            before_target = target.read_bytes()
            before_link = path.readlink()

            CodexManager(FakeRunner(), home).create_profiles()  # type: ignore[arg-type]
            self.assert_complete_readiness_ignores_unscoped_path(home)

            self.assertTrue(path.is_symlink())
            self.assertEqual(path.readlink(), before_link)
            self.assertEqual(target.read_bytes(), before_target)

    def test_managed_setup_creates_only_scoped_launchers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            CodexManager(FakeRunner(), home).create_profiles()  # type: ignore[arg-type]
            self.assertFalse((home / ".local/bin/codex").exists())
            for name in ("codex-01", "codex-02"):
                launcher = home / ".local/bin" / name
                self.assertTrue(launcher.is_file())
                self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
