from __future__ import annotations

import getpass
import os
from pathlib import Path

from ai_setup.config.codex import CODEX_PROFILES, CodexManager
from ai_setup.config.git import GitConfigurator, GitIdentity
from ai_setup.config.github import GitHubConfigurator
from ai_setup.config.shell import ShellInfo, configure_path, detect_shell
from ai_setup.config.ssh import SSHManager
from ai_setup.config.ssh_inventory import github_correlated_local_keys
from ai_setup.errors import ApplicationError, ValidationError
from ai_setup.execution.runner import Command, CommandRunner
from ai_setup.execution.workspace import TemporaryWorkspace
from ai_setup.identity import IDENTITY
from ai_setup.models import (
    STAGE_SPECS,
    Capability,
    Package,
    PackageKind,
    Plan,
    RunOptions,
    Source,
    WorkflowProgress,
    WorkflowStage,
    stage_spec,
)
from ai_setup.packages.managers import AurManager, FlatpakManager, PacmanManager, flathub_readiness
from ai_setup.planning.state import StateInspector
from ai_setup.system import validate_system
from ai_setup.ui.terminal import Terminal
from ai_setup.verification import readiness

SOURCE_LABELS = {
    Source.PACMAN: "Arch Linux",
    Source.AUR: "the AUR",
    Source.FLATPAK: "Flatpak",
    Source.UPSTREAM: "the official upstream release",
}

PLAN_ACTIONS = {
    WorkflowStage.ADMINISTRATOR: "Request administrator access.",
    WorkflowStage.SYSTEM: "Update Arch Linux.",
    WorkflowStage.APPLICATIONS: "Install or verify applications.",
    WorkflowStage.FLATPAK: "Configure or verify Flatpak and Flathub.",
    WorkflowStage.GIT: "Configure or verify Git.",
    WorkflowStage.GITHUB: "Configure or verify GitHub access.",
    WorkflowStage.SSH: "Configure or verify GitHub SSH access.",
    WorkflowStage.CODEX: "Configure or verify both Codex profiles.",
    WorkflowStage.SHELL: "Configure or verify the shell PATH.",
    WorkflowStage.VERIFICATION: "Verify the selected workstation state.",
}

FOCUSED_COMMANDS = {
    Capability.APPS: "apps",
    Capability.GIT: "git",
    Capability.GITHUB: "github",
    Capability.SSH: "ssh",
    Capability.CODEX: "codex",
}


class Workflow:
    def __init__(
        self,
        plan: Plan,
        options: RunOptions,
        terminal: Terminal,
        *,
        runner: CommandRunner | None = None,
        target_shell: ShellInfo | None = None,
        system_release: Path = Path("/etc/os-release"),
    ) -> None:
        self.plan = plan
        self.options = options
        self.terminal = terminal
        self.runner = runner or CommandRunner(
            dry_run=options.dry_run,
            verbose=options.verbose,
            output=terminal.output,
        )
        self.target_shell = target_shell
        self.system_release = system_release
        self.progress = WorkflowProgress(())
        self._pending_before: tuple[Package, ...] = ()
        self._pending_after_update: tuple[Package, ...] = ()

    def _shell(self) -> ShellInfo:
        if self.target_shell is None:
            self.target_shell = detect_shell()
        return self.target_shell

    def _capabilities(self) -> set[Capability]:
        return set(self.plan.selected) | set(self.plan.prerequisites)

    def _selected_stages(self, pending: tuple[Package, ...]) -> tuple[WorkflowStage, ...]:
        capabilities = self._capabilities()
        privileged = any(package.source in {Source.PACMAN, Source.AUR} for package in pending)
        return tuple(
            spec.stage
            for spec in STAGE_SPECS
            if spec.selected(capabilities, native_packages_pending=privileged)
        )

    def run(self) -> int:
        try:
            validate_system(require_network=not self.options.dry_run)
            if self.options.verbose:
                shell = self._shell()
                self.terminal.output(f"Environment: Arch Linux, {shell.name}.")
            inspector = StateInspector(self.runner, self.options.home)
            self._pending_before = inspector.pending(self.plan.packages)
            self._pending_after_update = self._pending_before
            self.progress = WorkflowProgress(self._selected_stages(self._pending_before))
            self._render_plan(self._pending_before)
            if self.options.dry_run:
                self.terminal.output("Dry run: no changes will be made.")
                self._render_verbose_plan(self._pending_before)
                return 0
            if not self.terminal.confirm("Continue?", assume_yes=self.options.assume_yes):
                self.terminal.output("")
                self.terminal.output("No changes were made.")
                return 0
            with TemporaryWorkspace(keep=self.options.keep_temp) as workspace:
                if self.options.verbose and workspace.path is not None:
                    self.terminal.output(f"Temporary workspace: {workspace.path}.")
                self._mutate(workspace.path or Path("/tmp"), inspector)
                self._verify()
            self._render_completion()
            return 0
        except KeyboardInterrupt:
            self._render_interruption()
            return 130
        except ApplicationError as exc:
            self._render_error(exc)
            return exc.exit_code

    @staticmethod
    def _noun(count: int, singular: str, plural: str | None = None) -> str:
        return singular if count == 1 else (plural or singular + "s")

    @staticmethod
    def _number(count: int) -> str:
        words = {
            0: "zero",
            1: "one",
            2: "two",
            3: "three",
            4: "four",
            5: "five",
            6: "six",
            7: "seven",
            8: "eight",
            9: "nine",
            10: "ten",
            11: "eleven",
            12: "twelve",
            13: "thirteen",
            14: "fourteen",
            15: "fifteen",
        }
        return words.get(count, str(count))

    @staticmethod
    def _join(items: list[str]) -> str:
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return ", ".join(items[:-1]) + f", and {items[-1]}"

    def _applications(self) -> tuple[Package, ...]:
        return tuple(
            package for package in self.plan.packages if package.kind is PackageKind.APPLICATION
        )

    def _render_plan(self, pending: tuple[Package, ...]) -> None:
        self.terminal.section("Plan")
        for stage in self.progress.selected:
            self.terminal.output(PLAN_ACTIONS[stage])
        applications = set(self._applications())
        source_order = {Source.PACMAN: 0, Source.AUR: 1, Source.FLATPAK: 2, Source.UPSTREAM: 3}
        missing = tuple(
            sorted(
                (package for package in pending if package in applications),
                key=lambda package: (source_order[package.source], package.name),
            )
        )
        self.terminal.output("")
        if len(missing) == 1:
            package = missing[0]
            self.terminal.output(
                f"Missing application: {package.name} from {SOURCE_LABELS[package.source]}."
            )
        elif missing:
            self.terminal.output("Missing applications:")
            self.terminal.output("")
            for package in missing:
                self.terminal.output(f"{package.name} from {SOURCE_LABELS[package.source]}.")
        total = len(self.plan.packages)
        present = total - len(pending)
        if not pending:
            self.terminal.output("All software requirements are already present.")
        elif total:
            self.terminal.output(
                f"{self._number(present).capitalize()} of {self._number(total)} software "
                f"{self._noun(total, 'requirement')} are already present."
            )
        if not self.options.assume_yes:
            self.terminal.output("")

    def _render_verbose_plan(self, pending: tuple[Package, ...]) -> None:
        if not self.options.verbose:
            return
        self.terminal.output(
            "Selected capabilities: " + ", ".join(c.value for c in self.plan.selected) + "."
        )
        for package in self.plan.packages:
            state = "pending" if package in pending else "present"
            self.terminal.output(f"{package.source.value}: {package.identifier} ({state}).")

    def _begin(self, stage: WorkflowStage) -> None:
        self.progress.begin(stage)
        self.terminal.section(stage_spec(stage).plan_label)

    def _finish(self, stage: WorkflowStage) -> None:
        self.progress.finish(stage)

    def _mutate(self, workspace: Path, inspector: StateInspector) -> None:
        pacman = PacmanManager(self.runner, workspace)
        if WorkflowStage.ADMINISTRATOR in self.progress.selected:
            self._begin(WorkflowStage.ADMINISTRATOR)
            self.terminal.output("Sudo will ask for your password.")
            self.progress.mutation_started = True
            self.runner.run(Command(("sudo", "-v")))
            self._finish(WorkflowStage.ADMINISTRATOR)
        if WorkflowStage.SYSTEM in self.progress.selected:
            self._begin(WorkflowStage.SYSTEM)
            self.terminal.output("Updating Arch Linux...")
            changed = pacman.full_update()
            self.terminal.output(
                "System updated." if changed else "The system is already up to date."
            )
            self._pending_after_update = inspector.pending(self._pending_before)
            self._finish(WorkflowStage.SYSTEM)
        if WorkflowStage.APPLICATIONS in self.progress.selected:
            self._begin(WorkflowStage.APPLICATIONS)
            self._install_applications(workspace, pacman, inspector)
            self._finish(WorkflowStage.APPLICATIONS)
        if WorkflowStage.FLATPAK in self.progress.selected:
            self._begin(WorkflowStage.FLATPAK)
            self._flatpak(inspector)
            self._finish(WorkflowStage.FLATPAK)
        if WorkflowStage.GIT in self.progress.selected:
            self._begin(WorkflowStage.GIT)
            self._git()
            self._finish(WorkflowStage.GIT)
        if WorkflowStage.GITHUB in self.progress.selected:
            self._begin(WorkflowStage.GITHUB)
            self._github()
            self._finish(WorkflowStage.GITHUB)
        if WorkflowStage.SSH in self.progress.selected:
            self._begin(WorkflowStage.SSH)
            self._ssh()
            self._finish(WorkflowStage.SSH)
        if WorkflowStage.CODEX in self.progress.selected:
            self._begin(WorkflowStage.CODEX)
            self._codex(workspace)
            self._finish(WorkflowStage.CODEX)
        if WorkflowStage.SHELL in self.progress.selected:
            self._begin(WorkflowStage.SHELL)
            shell = self._shell()
            update = configure_path(self.options.home, shell)
            if update.changed:
                self.terminal.output(f"Added ~/.local/bin to the {shell.name} PATH.")
                if update.new_session_required:
                    self.terminal.output("Open a new shell session to use the command.")
                else:
                    self.terminal.output("The current shell can already use the command.")
            else:
                self.terminal.output(f"The {shell.name} PATH already includes ~/.local/bin.")
            self._finish(WorkflowStage.SHELL)

    def _install_applications(
        self, workspace: Path, pacman: PacmanManager, inspector: StateInspector
    ) -> None:
        initial_apps = tuple(
            package
            for package in self._pending_before
            if package.kind is PackageKind.APPLICATION
            and package.source in {Source.PACMAN, Source.AUR}
        )
        pending = inspector.pending(self._pending_after_update)
        current_apps = tuple(
            package
            for package in pending
            if package.kind is PackageKind.APPLICATION
            and package.source in {Source.PACMAN, Source.AUR}
        )
        satisfied = tuple(package for package in initial_apps if package not in current_apps)
        for package in satisfied:
            self.terminal.output(f"{package.name} was installed during the system update.")
        native = tuple(p for p in pending if p.source is Source.PACMAN)
        aur = tuple(p for p in pending if p.source is Source.AUR and p.identifier != "yay-bin")
        yay_pending = any(p.source is Source.AUR and p.identifier == "yay-bin" for p in pending)
        install_apps = tuple(p for p in (*native, *aur) if p.kind is PackageKind.APPLICATION)
        pending_dependencies = tuple(p for p in pending if p.kind is PackageKind.DEPENDENCY)
        if pending_dependencies:
            self.terminal.output("Installing required software...")
        if not pending_dependencies and not initial_apps and not install_apps:
            self.terminal.output("All selected applications are already installed.")
        elif satisfied and not install_apps:
            self.terminal.output("No additional package installation was needed.")
        elif len(install_apps) == 1:
            self.terminal.output(f"Installing {install_apps[0].name}...")
        elif install_apps:
            self.terminal.output(f"Installing {self._number(len(install_apps))} applications.")
        elif pending and not pending_dependencies:
            self.terminal.output("Installing required software...")
        self.progress.mutation_started = self.progress.mutation_started or bool(pending)
        pacman.install(p.identifier for p in native)
        if aur or yay_pending:
            manager = AurManager(self.runner, workspace)
            readiness = inspector.aur_helper_readiness()
            if readiness.dependency_satisfied and not readiness.executable_runnable:
                raise ValidationError(
                    "Applications",
                    "inspect AUR helper",
                    "installed AUR helper is not runnable; existing provider was preserved",
                )
            if not readiness.ready:
                pacman.install(("git", "base-devel"))
                manager.bootstrap_yay()
            manager.install(p.identifier for p in aur)
        remaining = inspector.pending(tuple((*native, *aur)))
        for package in install_apps:
            if package not in remaining:
                self.terminal.output(f"{package.name} installed.")

    def _flatpak(self, inspector: StateInspector) -> None:
        manager = FlatpakManager(self.runner, self.options.home)
        readiness = flathub_readiness(self.runner, self.options.home)
        if readiness.configured:
            self.terminal.output("Flathub is already configured.")
        else:
            self.terminal.output("Configuring Flathub...")
        changed = manager.ensure_flathub(readiness)
        if changed:
            self.terminal.output("Flathub configured.")
        applications = tuple(
            p
            for p in self.plan.packages
            if p.kind is PackageKind.APPLICATION and p.source is Source.FLATPAK
        )
        pending = inspector.pending(applications)
        if not pending:
            self.terminal.output("All selected Flatpak applications are already installed.")
            return
        for package in pending:
            self.terminal.output(f"Installing {package.name}...")
        manager.install(p.identifier for p in pending)
        remaining = inspector.pending(pending)
        for package in pending:
            if package not in remaining:
                self.terminal.output(f"{package.name} installed.")

    def _git(self) -> None:
        git = GitConfigurator(self.runner)
        existing_name = git.get("user.name")
        existing_email = git.get("user.email")
        existing = bool(existing_name and existing_email)
        if existing:
            original = GitIdentity(existing_name or "", existing_email or "")
            self.terminal.output(f"Name: {original.name}")
            self.terminal.output(f"Email: {original.email}")
            if self.terminal.confirm(
                "Keep this identity?", default=True, assume_yes=self.options.assume_yes
            ):
                git.configure(original)
                self.terminal.output("Git identity unchanged.")
                return
            replacement = GitIdentity(
                self._identity_value("New name: ", "name"),
                self._identity_value("New email: ", "email"),
            )
            if self.terminal.confirm("Use this identity?", default=True):
                git.configure(replacement)
                self.terminal.output("Git identity updated.")
            else:
                git.configure(original)
                self.terminal.output("Git identity unchanged.")
            return

        identity = GitIdentity(
            self._identity_value("Name: ", "name"),
            self._identity_value("Email: ", "email"),
        )
        if self.terminal.confirm(
            "Use this identity?", default=True, assume_yes=self.options.assume_yes
        ):
            git.configure(identity)
            self.terminal.output("Git identity saved.")

    def _identity_value(self, prompt: str, field: str) -> str:
        value = self.terminal.input(prompt).strip()
        if not value:
            raise ValidationError("Git", "configure identity", f"{field} cannot be empty")
        return value

    def _github(self) -> None:
        github = GitHubConfigurator(self.runner)
        authenticated = github.authenticated()
        protocol = github.protocol()
        if authenticated:
            account = github.account() or "the configured account"
            self.terminal.output(f"Already signed in as {account}.")
        else:
            self.terminal.output("Starting manual authentication.")
            self.terminal.output(
                "Open the URL shown below in your browser and enter the displayed code."
            )
        github.authenticate(authenticated=authenticated, protocol=protocol)
        if not authenticated:
            account = github.account() or "the authenticated account"
            self.terminal.output(f"Signed in as {account}.")
        if protocol == "ssh":
            self.terminal.output("Git protocol already uses SSH.")
        else:
            self.terminal.output("Git protocol changed to SSH.")

    def _ssh(self) -> None:
        manager = SSHManager(self.runner, self.options.home)
        inventory = manager.inventory()
        existing = inventory.keys
        if self.options.verbose:
            for entry in inventory.unsafe:
                self.terminal.output(f"Preserved SSH entry {entry.name}: {entry.reason}.")
        remote_existing = manager.inventory_remote()
        account = GitHubConfigurator(self.runner).account() or getpass.getuser()
        dedicated_before = any(key.private_name == manager.key.name for key in existing)
        if dedicated_before:
            self.terminal.output("The dedicated key already exists.")
        else:
            self.terminal.output("Creating a dedicated SSH key...")
        created = manager.create(
            GitConfigurator(self.runner).get("user.email") or f"{account}@users.noreply.github.com"
        )
        if created:
            self.terminal.output("The dedicated key was created.")
        dedicated = next(
            (key for key in manager.inventory().keys if key.private_name == manager.key.name),
            None,
        )
        registered = bool(
            dedicated
            and dedicated.fingerprint
            and any(key.fingerprint == dedicated.fingerprint for key in remote_existing)
        )
        if registered:
            self.terminal.output("The key is registered with GitHub.")
        else:
            self.terminal.output("Registering the key with GitHub...")
            manager.upload(f"{IDENTITY.command_name}-{os.uname().nodename}")
            self.terminal.output("The key was registered with GitHub.")
        if not manager.verify():
            raise ValidationError("SSH", "verify GitHub connection", "authentication failed")
        self.terminal.output("The GitHub connection was verified.")
        old = tuple(key for key in existing if not key.protected)
        deleted_count = 0
        if old and not self.terminal.confirm(
            "Keep existing keys?", default=True, assume_yes=self.options.assume_yes
        ):
            eligible_old = github_correlated_local_keys(old, remote_existing)
            for key in eligible_old:
                self.terminal.output(
                    f"Eligible local key: {manager.ssh_dir / key.private_name} ({key.fingerprint})."
                )
            if not eligible_old:
                self.terminal.output(
                    "No old local keys are correlated with GitHub; nothing was deleted."
                )
            elif self.terminal.confirm("Delete these keys?", destructive=True):
                fingerprints = frozenset(key.fingerprint for key in eligible_old if key.fingerprint)
                remote_old = tuple(k for k in remote_existing if k.fingerprint in fingerprints)
                manager.validate_deletion(eligible_old)
                manager.delete_remote(
                    remote_old,
                    eligible_fingerprints=fingerprints,
                    explicit_confirmation=True,
                )
                manager.delete(eligible_old, explicit_confirmation=True)
                deleted_count = len(eligible_old)
                if not manager.verify():
                    raise ValidationError("SSH", "reverify dedicated key", "authentication failed")
        if old and len(old) > deleted_count:
            self.terminal.output("Existing SSH keys were preserved.")

    def _codex(self, workspace: Path) -> None:
        codex = CodexManager(self.runner, self.options.home, workspace)
        unrelated = codex.unrelated_codex()
        if unrelated is not None and self.options.verbose:
            self.terminal.output(f"Unrelated Codex installation preserved: {unrelated}.")
        if not codex.executable_valid():
            codex.install()
        codex.create_profiles()
        for profile in CODEX_PROFILES:
            if codex.verified(profile.identifier):
                self.terminal.output(f"{profile.display_label} is already signed in.")
                continue
            self.terminal.output(f"{profile.display_label} is not signed in.")
            device_auth = codex.device_auth_supported(profile.identifier)
            if device_auth:
                self.terminal.output(
                    f"Starting manual device authentication for {profile.display_label}."
                )
                self.terminal.output(
                    "Open the URL shown below in your browser and enter the displayed code."
                )
            else:
                self.terminal.output(
                    f"Device authentication is unavailable for {profile.display_label}."
                )
                self.terminal.output(f"Starting manual authentication for {profile.display_label}.")
                self.terminal.output("Open the URL shown below in your browser.")
            codex.authenticate(profile.identifier, device_auth=device_auth)
            self.terminal.output(f"{profile.display_label} signed in.")
        self.terminal.output("Both Codex profiles are ready.")

    def _verify(self) -> None:
        self._begin(WorkflowStage.VERIFICATION)
        scope = readiness.ReadinessScope.from_plan(self.plan)
        results = readiness.ReadinessVerifier(
            scope,
            self.runner,
            self.options.home,
            target_shell=self.target_shell,
            system_release=self.system_release,
        ).results()
        failures = [result for result in results if not result.passed]
        if failures:
            visible = failures if self.options.verbose else failures[:3]
            reason = "; ".join(f"{item.name}: {item.reason}" for item in visible)
            if len(visible) < len(failures):
                reason += f"; and {len(failures) - len(visible)} more checks"
            raise ValidationError("Verification", "inspect workstation", reason)
        readiness.render_readiness(scope, self.terminal)
        self.terminal.output("All verification checks passed.")
        self._finish(WorkflowStage.VERIFICATION)

    def _render_completion(self) -> None:
        self.terminal.output("")
        if set(self.plan.selected) == set(Capability):
            self.terminal.output("Setup complete.")
            self.terminal.output("Workstation ready.")
        else:
            self.terminal.output("Command complete.")
            self.terminal.output("Selected configuration is ready.")

    def _rerun_command(self, stage: WorkflowStage | None = None) -> str:
        stage_commands = {
            WorkflowStage.APPLICATIONS: "apps",
            WorkflowStage.GIT: "git",
            WorkflowStage.GITHUB: "github",
            WorkflowStage.SSH: "ssh",
            WorkflowStage.CODEX: "codex",
        }
        if stage in stage_commands:
            return f"{IDENTITY.command_name} {stage_commands[stage]}"
        if len(self.plan.selected) == 1:
            command = FOCUSED_COMMANDS.get(self.plan.selected[0])
            if command is not None:
                return f"{IDENTITY.command_name} {command}"
        return f"{IDENTITY.command_name} setup"

    def _later_stages(self, current: WorkflowStage | None) -> tuple[WorkflowStage, ...]:
        if current is None or current not in self.progress.selected:
            return self.progress.remaining
        index = self.progress.selected.index(current)
        return tuple(
            stage
            for stage in self.progress.selected[index + 1 :]
            if stage not in self.progress.completed
        )

    def _render_progress_summary(self, current: WorkflowStage | None, *, interrupted: bool) -> None:
        self.terminal.output("")
        if current is None:
            self.terminal.output(
                "Setup was interrupted before any stage ran."
                if interrupted
                else "Setup stopped before any stage ran."
            )
        else:
            verb = "interrupted during" if interrupted else "stopped at"
            self.terminal.output(f"Setup {verb} {current.value}.")
        if self.progress.completed:
            labels = [stage.value for stage in self.progress.completed]
            self.terminal.output(f"Earlier completed stages remain valid: {self._join(labels)}.")
        else:
            self.terminal.output("No earlier stages completed.")
        later = self._later_stages(current)
        if later:
            self.terminal.output(
                "Later stages did not run: " + self._join([stage.value for stage in later]) + "."
            )
        else:
            self.terminal.output("No later stages ran.")
        command = self._rerun_command(current)
        if interrupted:
            self.terminal.output(f"Run {command} to continue.")
        else:
            self.terminal.output(f"Resolve the reported problem, then run {command}.")
            self.terminal.output(f"Run {command} --verbose for diagnostic output.")

    def _render_interruption(self) -> None:
        if (
            self.progress.current is None
            and not self.progress.completed
            and not self.progress.mutation_started
        ):
            self.terminal.output("")
            self.terminal.output("Setup cancelled. No changes were made.")
            self.terminal.output(f"Run {self._rerun_command()} to try again.")
            return
        self._render_progress_summary(self.progress.current, interrupted=True)

    def _render_error(self, error: ApplicationError) -> None:
        if error.packages:
            by_identifier = {package.identifier: package.name for package in self.plan.packages}
            names = tuple(by_identifier.get(package, package) for package in error.packages)
            if self.progress.current is WorkflowStage.APPLICATIONS and len(names) == 1:
                self.terminal.output(f"{names[0]} could not be installed.")
            else:
                self.terminal.output(f"{error.component.rstrip('.')} failed.")
            self.terminal.output(f"Reason: {error.reason.rstrip('.')}.")
            if error.log_path:
                self.terminal.output(f"Details: {error.log_path}.")
        else:
            self.terminal.output(
                f"{error.component.rstrip('.')} failed while trying to {error.operation}: "
                f"{error.reason.rstrip('.')}."
            )
            if error.log_path:
                self.terminal.output(f"Details: {error.log_path}.")
        self._render_progress_summary(self.progress.current, interrupted=False)
