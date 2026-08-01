from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from ai_setup.catalog.loader import load_catalog
from ai_setup.config.codex import CodexManager
from ai_setup.config.shell import ShellInfo, configure_path
from ai_setup.errors import ValidationError
from ai_setup.execution.runner import Command, CommandResult
from ai_setup.models import Capability, Package, Plan, RunOptions, Source, WorkflowStage
from ai_setup.planning.state import StateInspector
from ai_setup.ui.terminal import Terminal
from ai_setup.verification.checks import CheckResult
from ai_setup.workflow import Workflow
from tests.helpers import FakeRunner, controlled_executable_lookup


def response_for(package: Package, installed: bool) -> tuple[tuple[str, ...], CommandResult] | None:
    code = 0 if installed else 1
    if package.source is Source.AUR and package.identifier == "yay-bin":
        return None
    if package.source in {Source.PACMAN, Source.AUR}:
        argv = ("pacman", "-Q", package.identifier)
    elif package.source is Source.FLATPAK:
        argv = ("flatpak", "info", "--user", package.identifier)
    else:
        return None
    return argv, CommandResult(argv, code, "", "")


class StateAndUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            controlled_executable_lookup(
                {
                    "flatpak": "/usr/bin/flatpak",
                    "yay": "/usr/bin/yay",
                }
            )
        )
        self.catalog = load_catalog()
        self.packages = tuple(sorted((*self.catalog.apps, *self.catalog.deps)))

    def inspector(self, home: Path, installed_ids: set[str]) -> StateInspector:
        responses = dict(
            item
            for package in self.packages
            if (item := response_for(package, package.identifier in installed_ids)) is not None
        )
        yay_ready = "yay-bin" in installed_ids
        for argv in (
            ("pacman", "-T", "yay"),
            ("pacman", "-Qo", "--", "/usr/bin/yay"),
            ("/usr/bin/yay", "--version"),
        ):
            responses[argv] = CommandResult(argv, 0 if yay_ready else 1, "", "")
        if "org.vinegarhq.Sober" in installed_ids:
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n", encoding="utf-8")
        if "codex" in installed_ids:
            shared = home / ".local/share/ai/bin/codex"
            shared.parent.mkdir(parents=True)
            shared.write_text("binary", encoding="utf-8")
        return StateInspector(FakeRunner(responses), home)  # type: ignore[arg-type]

    def test_no_packages_installed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pending = self.inspector(Path(raw), set()).pending(self.packages)
            self.assertEqual(len(pending), 16)

    def test_all_packages_installed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            ids = {package.identifier for package in self.packages}
            self.assertEqual(self.inspector(Path(raw), ids).pending(self.packages), ())

    def test_installed_steam_focused_rerun_produces_no_install_command(self) -> None:
        steam = next(
            package for package in self.packages if package.identifier == "com.valvesoftware.Steam"
        )
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n", encoding="utf-8")
            remotes = ("flatpak", "remotes", "--user", "--columns=name")
            info = ("flatpak", "info", "--user", "com.valvesoftware.Steam")
            runner = FakeRunner(
                {
                    remotes: CommandResult(remotes, 0, "flathub\n", ""),
                    info: CommandResult(info, 0, "", ""),
                }
            )
            workflow = Workflow(
                Plan((Capability.APPS,), (Capability.FLATPAK, Capability.FLATHUB), (steam,)),
                RunOptions(home=home),
                Terminal(output=lambda _: None),
                runner=runner,  # type: ignore[arg-type]
            )
            inspector = StateInspector(runner, home)  # type: ignore[arg-type]
            workflow._flatpak(inspector)
            workflow._flatpak(inspector)
            self.assertFalse(
                any(command.argv[:2] == ("flatpak", "install") for command in runner.commands)
            )
            self.assertEqual([command.argv for command in runner.commands].count(info), 2)

    def test_source_yay_provider_satisfies_preferred_yay_bin_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = next(p for p in self.packages if p.identifier == "yay-bin")
            pacman_test = ("pacman", "-T", "yay")
            version = ("/usr/bin/yay", "--version")
            owner = ("pacman", "-Qo", "--", "/usr/bin/yay")
            exact_query = ("pacman", "-Q", "yay-bin")
            runner = FakeRunner(
                {
                    pacman_test: CommandResult(pacman_test, 0, "", ""),
                    owner: CommandResult(owner, 0, "yay-bin owns /usr/bin/yay\n", ""),
                    version: CommandResult(version, 0, "yay v13.0.1\n", ""),
                    exact_query: CommandResult(exact_query, 1, "", "not found"),
                }
            )
            inspector = StateInspector(runner, Path(raw))  # type: ignore[arg-type]
            self.assertEqual(inspector.pending((package,)), ())
            self.assertNotIn(exact_query, [command.argv for command in runner.commands])

    def test_unowned_yay_executable_does_not_satisfy_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            package = next(p for p in self.packages if p.identifier == "yay-bin")
            responses = {
                ("pacman", "-T", "yay"): CommandResult((), 1, "", "missing"),
                ("/usr/bin/yay", "--version"): CommandResult((), 0, "yay v13.0.1\n", ""),
            }
            inspector = StateInspector(FakeRunner(responses), Path(raw))  # type: ignore[arg-type]
            self.assertEqual(inspector.pending((package,)), (package,))

    def test_valid_yay_provider_does_not_select_applications_or_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            package = next(p for p in self.packages if p.identifier == "yay-bin")
            responses = {
                ("pacman", "-T", "yay"): CommandResult((), 0, "", ""),
                ("pacman", "-Qo", "--", "/usr/bin/yay"): CommandResult((), 0, "", ""),
                ("/usr/bin/yay", "--version"): CommandResult((), 0, "yay v13.0.1\n", ""),
            }
            runner = FakeRunner(responses)
            workflow = Workflow(
                Plan((Capability.DEPS,), (), (package,)),
                RunOptions(home=home),
                Terminal(output=lambda _: None),
                runner=runner,  # type: ignore[arg-type]
            )
            inspector = StateInspector(runner, home)  # type: ignore[arg-type]
            pending = inspector.pending((package,))
            self.assertNotIn(WorkflowStage.APPLICATIONS, workflow._selected_stages(pending))
            workflow._pending_before = pending
            workflow._pending_after_update = pending
            with patch("ai_setup.workflow.AurManager") as manager:
                workflow._install_applications(home, Mock(), inspector)
            manager.assert_not_called()

    def test_missing_yay_provider_triggers_preferred_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            package = next(p for p in self.packages if p.identifier == "yay-bin")
            responses = {
                ("pacman", "-T", "yay"): CommandResult((), 1, "", "missing"),
                ("yay", "--version"): CommandResult((), 1, "", "missing"),
            }
            runner = FakeRunner(responses)
            workflow = Workflow(
                Plan((Capability.DEPS,), (), (package,)),
                RunOptions(home=home),
                Terminal(output=lambda _: None),
                runner=runner,  # type: ignore[arg-type]
            )
            inspector = StateInspector(runner, home)  # type: ignore[arg-type]
            workflow._pending_before = (package,)
            workflow._pending_after_update = (package,)
            manager = Mock()
            pacman = Mock()
            with patch("ai_setup.workflow.AurManager", return_value=manager):
                workflow._install_applications(home, pacman, inspector)
            self.assertIn(call(("git", "base-devel")), pacman.install.call_args_list)
            manager.bootstrap_yay.assert_called_once_with()

    def test_broken_installed_yay_provider_is_preserved_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            package = next(p for p in self.packages if p.identifier == "yay-bin")
            responses = {
                ("pacman", "-T", "yay"): CommandResult((), 0, "", ""),
                ("pacman", "-Qo", "--", "/usr/bin/yay"): CommandResult((), 0, "", ""),
                ("/usr/bin/yay", "--version"): CommandResult((), 1, "", "broken"),
            }
            runner = FakeRunner(responses)
            output: list[str] = []
            workflow = Workflow(
                Plan((Capability.DEPS,), (), (package,)),
                RunOptions(home=home),
                Terminal(output=output.append),
                runner=runner,  # type: ignore[arg-type]
            )
            inspector = StateInspector(runner, home)  # type: ignore[arg-type]
            workflow._pending_before = (package,)
            workflow._pending_after_update = (package,)
            manager = Mock()
            with (
                patch("ai_setup.workflow.AurManager", return_value=manager),
                self.assertRaisesRegex(ValidationError, "installed AUR helper is not runnable"),
            ):
                workflow._install_applications(home, Mock(), inspector)
            manager.bootstrap_yay.assert_not_called()
            self.assertEqual(output, ["Installing required software..."])

    def test_only_git_installed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pending = self.inspector(Path(raw), {"git"}).pending(self.packages)
            self.assertNotIn("git", {package.identifier for package in pending})
            self.assertEqual(len(pending), 15)

    def test_rerun_skips_requirements_completed_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            completed = {"discord", "mullvad-vpn", "spotify-launcher"}
            pending = self.inspector(Path(raw), completed).pending(self.packages)
            pending_ids = {package.identifier for package in pending}
            self.assertTrue(completed.isdisjoint(pending_ids))
            self.assertEqual(len(pending), len(self.packages) - len(completed))

    def test_git_and_openssh_present_github_cli_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            github_requirements = tuple(
                package
                for package in self.packages
                if package.identifier in {"git", "openssh", "github-cli"}
            )
            pending = self.inspector(Path(raw), {"git", "openssh"}).pending(github_requirements)
            self.assertEqual([package.identifier for package in pending], ["github-cli"])

    def test_codex_executable_present_but_launchers_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            codex = next(package for package in self.packages if package.identifier == "codex")
            self.assertEqual(self.inspector(home, {"codex"}).pending((codex,)), ())
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            self.assertFalse(manager.profiles_distinct())

    def test_invalid_codex_executable_remains_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            codex = next(package for package in self.packages if package.identifier == "codex")
            shared = home / ".local/share/ai/bin/codex"
            shared.parent.mkdir(parents=True)
            shared.write_text("broken", encoding="utf-8")
            argv = (str(shared), "--version")
            runner = FakeRunner({argv: CommandResult(argv, 1, "", "not executable")})
            self.assertEqual(StateInspector(runner, home).pending((codex,)), (codex,))  # type: ignore[arg-type]

    def test_launchers_present_but_one_profile_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            manager = CodexManager(FakeRunner(), home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            (manager.state_root / "02").rename(manager.state_root / "missing-02")
            self.assertFalse(manager.verified("02"))

    def test_codex_rerun_reuses_binary_and_skips_authenticated_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            workspace.mkdir()
            runner = FakeRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            workflow = Workflow(
                Plan((Capability.CODEX,), (), ()),
                RunOptions(home=home),
                Terminal(output=lambda _: None),
                runner=runner,  # type: ignore[arg-type]
            )
            workflow._codex(workspace)
            argv = [command.argv for command in runner.commands]
            self.assertFalse(any(command[0] == "curl" for command in argv))
            self.assertFalse(any(command[-1] == "login" for command in argv))

    def test_codex_profiles_are_checked_and_authenticated_independently(self) -> None:
        class ProfileRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.profile_two_authenticated = False

            def run(self, command: Command, *, check: bool = True) -> CommandResult:
                self.commands.append(command)
                argv = command.argv
                if argv[-2:] == ("login", "status"):
                    if "codex-01" in argv[0] or self.profile_two_authenticated:
                        return CommandResult(argv, 0, "", "")
                    return CommandResult(argv, 1, "", "not logged in")
                if argv[-2:] == ("login", "--device-auth") or argv[-1:] == ("login",):
                    self.profile_two_authenticated = True
                if argv[-2:] == ("login", "--help"):
                    return CommandResult(argv, 0, "--device-auth\n", "")
                return CommandResult(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            workspace = home / "workspace"
            workspace.mkdir()
            runner = ProfileRunner()
            manager = CodexManager(runner, home)  # type: ignore[arg-type]
            manager.shared_bin.parent.mkdir(parents=True)
            manager.shared_bin.write_text("binary", encoding="utf-8")
            manager.create_profiles()
            output: list[str] = []
            workflow = Workflow(
                Plan((Capability.CODEX,), (), ()),
                RunOptions(home=home),
                Terminal(output=output.append),
                runner=runner,  # type: ignore[arg-type]
            )
            with patch("ai_setup.config.codex.interactive_terminal_available", return_value=True):
                workflow._codex(workspace)
            login_commands = [command.argv for command in runner.commands if command.interactive]
            self.assertEqual(
                login_commands,
                [(str(manager.bin_dir / "codex-02"), "login", "--device-auth")],
            )
            self.assertIn("codex-01 is already signed in.", output)
            self.assertIn("codex-02 signed in.", output)

    def test_complete_workstation_package_inventory_has_no_pending_items(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            ids = {package.identifier for package in self.packages}
            inspector = self.inspector(home, ids)
            self.assertEqual(inspector.pending(self.packages), ())

    def test_complete_workstation_configuration_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            responses: dict[tuple[str, ...], CommandResult] = {}
            for package in self.packages:
                item = response_for(package, True)
                if item:
                    responses[item[0]] = item[1]
            remotes = ("flatpak", "remotes", "--user", "--columns=name")
            responses[remotes] = CommandResult(remotes, 0, "flathub\n", "")
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n", encoding="utf-8")
            for key, value in (
                ("user.name", "Person"),
                ("user.email", "person@example.com"),
                ("init.defaultBranch", "main"),
            ):
                argv = ("git", "config", "--global", "--get", key)
                responses[argv] = CommandResult(argv, 0, value + "\n", "")
            protocol = ("gh", "config", "get", "git_protocol", "--host", "github.com")
            responses[protocol] = CommandResult(protocol, 0, "ssh\n", "")
            ssh = ("ssh", "-T", "-o", "BatchMode=yes", "git@github.com")
            responses[ssh] = CommandResult(ssh, 1, "", "successfully authenticated")
            runner = FakeRunner(responses)
            codex = CodexManager(runner, home)  # type: ignore[arg-type]
            codex.shared_bin.parent.mkdir(parents=True)
            codex.shared_bin.write_text("binary", encoding="utf-8")
            codex.create_profiles()
            fish = ShellInfo("fish", Path("/usr/bin/fish"), "test")
            configure_path(home, fish, env={"PATH": ""})
            output: list[str] = []
            plan = Plan(tuple(Capability), (), self.packages)
            workflow = Workflow(
                plan,
                RunOptions(home=home),
                Terminal(output=output.append),
                runner=runner,  # type: ignore[arg-type]
                target_shell=fish,
            )
            with patch(
                "ai_setup.verification.readiness.Verifier.system",
                return_value=CheckResult("supported system", True),
            ):
                workflow._verify()
            self.assertIn("All verification checks passed.", output)

    def test_dry_run_distinguishes_requirements_and_pending_installations(self) -> None:
        output: list[str] = []
        plan = Plan((), (), self.packages)
        workflow = Workflow(
            plan,
            RunOptions(dry_run=True),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow.progress.selected = workflow._selected_stages(self.packages[:2])
        workflow._render_plan(self.packages[:2])
        self.assertIn("Fourteen of 16 software requirements are already present.", output)

    def test_vpn_dry_run_groups_official_package_as_system_software(self) -> None:
        vpn = next(package for package in self.packages if package.identifier == "mullvad-vpn")
        output: list[str] = []
        workflow = Workflow(
            Plan((Capability.APPS,), (), (vpn,)),
            RunOptions(dry_run=True, verbose=True),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow.progress.selected = workflow._selected_stages((vpn,))
        workflow._render_plan((vpn,))
        workflow._render_verbose_plan((vpn,))
        rendered = "\n".join(output)
        self.assertIn("Missing application: Mullvad VPN from Arch Linux.", rendered)
        self.assertIn("pacman: mullvad-vpn (pending).", rendered)
        self.assertNotIn("mullvad-vpn-bin", rendered)

    def test_normal_summary_uses_source_specific_plain_language(self) -> None:
        output: list[str] = []
        plan = Plan((), (), self.packages)
        workflow = Workflow(
            plan,
            RunOptions(),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        pending = tuple(
            package
            for package in self.packages
            if package.identifier in {"discord", "org.vinegarhq.Sober", "codex"}
        )
        workflow.progress.selected = workflow._selected_stages(pending)
        workflow._render_plan(pending)
        rendered = "\n".join(output)
        self.assertIn("Discord from Arch Linux.", rendered)
        self.assertIn("Sober from Flatpak.", rendered)
        self.assertIn("OpenAI Codex CLI from the official upstream release.", rendered)
        self.assertNotIn("✓", rendered)

    def test_final_summary_uses_execution_results(self) -> None:
        output: list[str] = []
        workflow = Workflow(
            Plan((), (), ()),
            RunOptions(),
            Terminal(output=output.append),
            runner=FakeRunner(),  # type: ignore[arg-type]
        )
        workflow._render_completion()
        self.assertEqual(
            output,
            [
                "",
                "Command complete.",
                "Selected configuration is ready.",
            ],
        )
