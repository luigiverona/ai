from __future__ import annotations

import os
import pty
import select
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_setup.errors import CommandError, ValidationError
from ai_setup.execution.runner import (
    Command,
    CommandResult,
    CommandRunner,
    manual_authentication_env,
)
from ai_setup.execution.workspace import TemporaryWorkspace
from ai_setup.packages.aur_source import (
    YAY_BIN_SOURCE,
    srcinfo_identity,
    tracked_tree_sha256,
)
from ai_setup.packages.managers import AurManager, FlatpakManager, PacmanManager
from tests.helpers import FakeRunner, controlled_executable_lookup


class ExecutionTests(unittest.TestCase):
    @patch("ai_setup.execution.runner.subprocess.run")
    def test_normal_commands_capture_output(self, run: Mock) -> None:
        run.return_value = Mock(returncode=0, stdout="out", stderr="err")
        result = CommandRunner().run(Command(("example",), mutate=False))
        self.assertEqual((result.stdout, result.stderr), ("out", "err"))
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertNotIn("stdin", run.call_args.kwargs)

    @patch("ai_setup.execution.runner.subprocess.run")
    def test_interactive_commands_inherit_terminal_streams(self, run: Mock) -> None:
        run.return_value = Mock(returncode=0)
        result = CommandRunner().run(Command(("example",), interactive=True))
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        kwargs = run.call_args.kwargs
        self.assertNotIn("capture_output", kwargs)
        self.assertIsNone(kwargs["stdin"])
        self.assertIsNone(kwargs["stdout"])
        self.assertIsNone(kwargs["stderr"])

    @patch("ai_setup.execution.runner.subprocess.run")
    def test_interactive_nonzero_has_scoped_reason_and_no_empty_log(self, run: Mock) -> None:
        run.return_value = Mock(returncode=9)
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "failure.log"
            with self.assertRaises(CommandError) as caught:
                CommandRunner().run(
                    Command(
                        ("example",),
                        interactive=True,
                        failure_component="GitHub",
                        failure_operation="authenticate",
                        log_path=log,
                    )
                )
            self.assertEqual(caught.exception.reason, "interactive command exited with status 9")
            self.assertFalse(log.exists())

    @patch("ai_setup.execution.runner.subprocess.run")
    def test_dry_run_skips_interactive_mutation(self, run: Mock) -> None:
        result = CommandRunner(dry_run=True).run(Command(("example",), interactive=True))
        self.assertEqual(result, CommandResult(("example",), 0, "", ""))
        run.assert_not_called()

    @patch("ai_setup.execution.runner.subprocess.run", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_is_not_converted(self, _: Mock) -> None:
        with self.assertRaises(KeyboardInterrupt):
            CommandRunner().run(Command(("example",), interactive=True))

    @patch("ai_setup.execution.runner.subprocess.run")
    def test_interactive_verbose_rendering_redacts_sensitive_arguments(self, run: Mock) -> None:
        run.return_value = Mock(returncode=0)
        output: list[str] = []
        CommandRunner(verbose=True, output=output.append).run(
            Command(("example", "secret"), interactive=True, sensitive_values=("secret",))
        )
        self.assertEqual(output, ["$ example [REDACTED]"])

    @patch("ai_setup.execution.runner.subprocess.run")
    @patch.dict(
        "os.environ",
        {
            "CODEX_HOME": "/test/codex-home",
            "HTTPS_PROXY": "https://proxy.invalid",
            "AI_WORKSTATION_INHERITED": "present",
            "PATH": "/test/path",
        },
        clear=True,
    )
    def test_manual_browser_environment_is_scoped_and_does_not_leak(self, run: Mock) -> None:
        run.return_value = Mock(returncode=0)
        original = dict(os.environ)
        runner = CommandRunner()
        runner.run(
            Command(
                ("authenticate",),
                env=manual_authentication_env(),
                interactive=True,
            )
        )
        runner.run(Command(("later",), mutate=False))
        authentication_env = run.call_args_list[0].kwargs["env"]
        later_env = run.call_args_list[1].kwargs["env"]
        self.assertEqual(authentication_env["BROWSER"], "/usr/bin/echo")
        self.assertEqual(authentication_env["GH_BROWSER"], "/usr/bin/echo")
        self.assertEqual(authentication_env["PATH"], "/test/path")
        self.assertEqual(authentication_env["CODEX_HOME"], "/test/codex-home")
        self.assertEqual(authentication_env["HTTPS_PROXY"], "https://proxy.invalid")
        self.assertEqual(authentication_env["AI_WORKSTATION_INHERITED"], "present")
        self.assertNotIn(
            authentication_env["BROWSER"],
            {"xdg-open", "gio", "firefox", "librewolf", "chromium"},
        )
        self.assertNotIn("BROWSER", later_env)
        self.assertNotIn("GH_BROWSER", later_env)
        self.assertEqual(dict(os.environ), original)

    @unittest.skipUnless(sys.platform.startswith("linux"), "pseudo-terminal test requires Linux")
    def test_real_interactive_path_exposes_output_and_accepts_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = Path(raw) / "fake_auth.py"
            fixture.write_text(
                "import os\n"
                "import subprocess\n"
                "import sys\n"
                "subprocess.run([os.environ['BROWSER'], "
                "'https://example.invalid/device'], check=True)\n"
                "print('CODE-1234', file=sys.stderr, flush=True)\n"
                "answer = input()\n"
                "raise SystemExit(0 if answer == 'continue' else 4)\n",
                encoding="utf-8",
            )
            pid, master = pty.fork()
            if pid == 0:
                try:
                    result = CommandRunner().run(
                        Command(
                            (sys.executable, str(fixture)),
                            env=manual_authentication_env(),
                            interactive=True,
                            mutate=False,
                        )
                    )
                    os._exit(0 if result.stdout == result.stderr == "" else 5)
                except BaseException:
                    os._exit(6)
            visible = b""
            sent = False
            while True:
                ready, _, _ = select.select([master], [], [], 5)
                self.assertTrue(ready, "interactive fixture hung")
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                visible += chunk
                if not sent and b"CODE-1234" in visible:
                    os.write(master, b"continue\n")
                    sent = True
            _, status = os.waitpid(pid, 0)
            os.close(master)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertIn(b"https://example.invalid/device", visible)
            self.assertIn(b"CODE-1234", visible)
            self.assertTrue(sent)
            self.assertEqual([path.name for path in Path(raw).iterdir()], ["fake_auth.py"])

    def test_dry_run_does_not_execute(self) -> None:
        runner = CommandRunner(dry_run=True)
        result = runner.run(Command(("definitely-missing-command",)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(runner.history), 1)

    def test_command_can_replace_inherited_environment(self) -> None:
        result = CommandRunner().run(
            Command(
                (
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('AI_WORKSTATION_POISON', ''))",
                ),
                env={"PATH": "/usr/bin:/bin"},
                replace_env=True,
                mutate=False,
            )
        )
        self.assertEqual(result.stdout, "\n")

    def test_redaction(self) -> None:
        self.assertEqual(CommandRunner.redact("token=secret", ("secret",)), "token=[REDACTED]")

    def test_verbose_command_and_output_are_redacted(self) -> None:
        output: list[str] = []
        runner = CommandRunner(verbose=True, output=output.append)
        runner.run(
            Command(
                ("printf", "%s", "secret"),
                sensitive_values=("secret",),
                mutate=False,
            )
        )
        self.assertNotIn("secret", "\n".join(output))
        self.assertIn("[REDACTED]", "\n".join(output))

    def test_package_failure_has_compact_reason_and_full_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "logs/aur.log"
            command = Command(
                (
                    sys.executable,
                    "-c",
                    "import sys; print(':: old and new are in conflict'); "
                    "print('error: failed to prepare transaction', file=sys.stderr); sys.exit(1)",
                ),
                mutate=False,
                failure_component="AUR installation",
                failure_operation="install packages",
                failure_packages=("example-bin",),
                log_path=log,
            )
            with self.assertRaises(CommandError) as caught:
                CommandRunner().run(command)
            self.assertEqual(caught.exception.reason, "old and new are in conflict")
            self.assertEqual(caught.exception.packages, ("example-bin",))
            self.assertEqual(caught.exception.log_path, str(log))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            content = log.read_text(encoding="utf-8")
            self.assertIn("old and new are in conflict", content)
            self.assertIn("failed to prepare transaction", content)

    def test_successful_command_output_stays_quiet_without_verbose(self) -> None:
        output: list[str] = []
        CommandRunner(output=output.append).run(
            Command((sys.executable, "-c", "print('ordinary package output')"), mutate=False)
        )
        self.assertEqual(output, [])

    def test_workspace_success_cleanup_and_keep(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with TemporaryWorkspace(temp_root=root) as workspace:
                path = workspace.path
                self.assertEqual(
                    {entry.name for entry in path.iterdir()},
                    {"aur", "downloads", "logs", "state"},
                )
            self.assertFalse(path.exists())
            with TemporaryWorkspace(temp_root=root, keep=True) as kept:
                kept_path = kept.path
            self.assertTrue(kept_path.exists())
            TemporaryWorkspace.safe_cleanup(kept_path, root)

    def test_failed_workspace_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(RuntimeError):
                with TemporaryWorkspace(temp_root=root) as workspace:
                    path = workspace.path
                    raise RuntimeError("fail")
            self.assertTrue(path.exists())

    def test_cleanup_guards(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(ValueError):
                TemporaryWorkspace.safe_cleanup(root, root)

    def test_pacman_command_is_sorted_and_full_update(self) -> None:
        runner = FakeRunner()
        manager = PacmanManager(runner)  # type: ignore[arg-type]
        manager.full_update()
        manager.install(("z", "a", "a"))
        self.assertEqual(
            runner.commands[0].argv, ("sudo", "pacman", "-Syu", "--noconfirm", "--needed")
        )
        self.assertEqual(runner.commands[0].env, {"LC_ALL": "C"})
        self.assertEqual(runner.commands[1].argv[-2:], ("a", "z"))

    def test_pacman_update_result_is_locale_independent(self) -> None:
        command = ("sudo", "pacman", "-Syu", "--noconfirm", "--needed")
        no_op = FakeRunner({command: CommandResult(command, 0, " there is nothing to do\n", "")})
        changed = FakeRunner({command: CommandResult(command, 0, "Packages (1) example-2.0\n", "")})
        self.assertFalse(PacmanManager(no_op).full_update())  # type: ignore[arg-type]
        self.assertTrue(PacmanManager(changed).full_update())  # type: ignore[arg-type]
        self.assertEqual(no_op.commands[0].env, {"LC_ALL": "C"})

    @patch.dict("os.environ", {"LANG": "fr_FR.UTF-8", "AI_WORKSTATION_INHERITED": "present"})
    def test_command_environment_overrides_locale_without_replacing_environment(self) -> None:
        result = CommandRunner().run(
            Command(
                (
                    sys.executable,
                    "-c",
                    "import os; print(os.environ['LC_ALL']); print(os.environ['AI_WORKSTATION_INHERITED'])",
                ),
                env={"LC_ALL": "C"},
                mutate=False,
            )
        )
        self.assertEqual(result.stdout, "C\npresent\n")

    @patch("ai_setup.packages.managers.os.geteuid", return_value=0)
    def test_aur_never_builds_as_root(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValidationError):
                AurManager(FakeRunner(), Path(raw)).bootstrap_yay()  # type: ignore[arg-type]

    @patch("ai_setup.packages.managers.os.geteuid", return_value=1000)
    @patch(
        "ai_setup.packages.managers.tracked_tree_sha256",
        return_value="a98e7e25d3c0b3a2ee92ba09bf72f44ee321922b02b2300caae6dac982583fe4",
    )
    def test_aur_validates_metadata_builds_unprivileged_and_elevates_only_install(
        self, tree_digest: Mock, _: object
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            clone = workspace / "aur/yay-bin"
            clone.mkdir(parents=True)
            (clone / ".SRCINFO").write_text(
                "pkgbase = yay-bin\npkgname = yay-bin\n", encoding="utf-8"
            )
            config = workspace / "state/makepkg.conf"
            system_config = workspace / "system-makepkg.conf"
            system_config.write_text("OPTIONS=(strip debug)\n", encoding="utf-8")
            head_argv = ("git", "-C", str(clone), "rev-parse", "HEAD")
            remotes_argv = ("git", "-C", str(clone), "remote")
            origin_argv = ("git", "-C", str(clone), "remote", "get-url", "origin")
            submodules_argv = (
                "git",
                "-C",
                str(clone),
                "submodule",
                "status",
                "--recursive",
            )
            index_argv = ("git", "-C", str(clone), "ls-files", "--stage", "-z")
            status_argv = (
                "git",
                "-C",
                str(clone),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored",
                "-z",
            )
            metadata_argv = ("makepkg", "--config", str(config), "--printsrcinfo")
            package_argv = ("makepkg", "--config", str(config), "--packagelist")
            artifact = str(clone / "yay-bin-13.0.1-1-x86_64.pkg.tar.zst")
            debug_artifact = str(clone / "yay-bin-debug-13.0.1-1-x86_64.pkg.tar.zst")
            unrelated_artifact = str(clone / "yay-helper-13.0.1-1-x86_64.pkg.tar.zst")
            responses = {
                head_argv: CommandResult(head_argv, 0, YAY_BIN_SOURCE.commit + "\n", ""),
                remotes_argv: CommandResult(remotes_argv, 0, "origin\n", ""),
                origin_argv: CommandResult(origin_argv, 0, YAY_BIN_SOURCE.repository + "\n", ""),
                submodules_argv: CommandResult(submodules_argv, 0, "", ""),
                index_argv: CommandResult(index_argv, 0, "index fixture", ""),
                status_argv: CommandResult(status_argv, 0, "", ""),
                metadata_argv: CommandResult(
                    metadata_argv, 0, "pkgbase = yay-bin\npkgname = yay-bin\n", ""
                ),
                package_argv: CommandResult(
                    package_argv,
                    0,
                    artifact + "\n" + debug_artifact + "\n" + unrelated_artifact + "\n",
                    "",
                ),
            }
            runner = FakeRunner(responses)
            with controlled_executable_lookup({"yay": "/usr/bin/yay"}):
                AurManager(runner, workspace, system_config).bootstrap_yay()  # type: ignore[arg-type]
            commands = [command.argv for command in runner.commands]
            self.assertEqual(
                commands[:4],
                [
                    ("git", "init", str(clone)),
                    (
                        "git",
                        "-C",
                        str(clone),
                        "remote",
                        "add",
                        "origin",
                        YAY_BIN_SOURCE.repository,
                    ),
                    (
                        "git",
                        "-C",
                        str(clone),
                        "fetch",
                        "--no-tags",
                        "--depth=1",
                        "origin",
                        YAY_BIN_SOURCE.commit,
                    ),
                    (
                        "git",
                        "-C",
                        str(clone),
                        "checkout",
                        "--detach",
                        YAY_BIN_SOURCE.commit,
                    ),
                ],
            )
            tree_digest.assert_called_once_with(clone, "index fixture", "")
            self.assertIn(
                ("makepkg", "--config", str(config), "--cleanbuild", "--noconfirm"),
                commands,
            )
            self.assertNotIn(("makepkg", "--syncdeps", "--cleanbuild", "--noconfirm"), commands)
            self.assertIn(("sudo", "pacman", "-U", "--noconfirm", artifact), commands)
            install = next(
                command for command in commands if command[:3] == ("sudo", "pacman", "-U")
            )
            self.assertNotIn(debug_artifact, install)
            self.assertNotIn(unrelated_artifact, install)
            install_index = commands.index(install)
            self.assertGreater(commands.index(("pacman", "-T", "yay")), install_index)
            self.assertGreater(commands.index(("/usr/bin/yay", "--version")), install_index)
            self.assertIn("OPTIONS[_ai_index]=!debug", config.read_text(encoding="utf-8"))

    def test_aur_pin_and_tree_digest_contract(self) -> None:
        self.assertEqual(YAY_BIN_SOURCE.repository, "https://aur.archlinux.org/yay-bin.git")
        self.assertEqual(YAY_BIN_SOURCE.commit, "13e0a4754d106a9252b7479bf1b370fbe454fc48")
        self.assertEqual(YAY_BIN_SOURCE.package_base, "yay-bin")
        self.assertEqual(YAY_BIN_SOURCE.package_names, ("yay-bin",))
        self.assertEqual(
            YAY_BIN_SOURCE.tree_sha256,
            "a98e7e25d3c0b3a2ee92ba09bf72f44ee321922b02b2300caae6dac982583fe4",
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "dir").mkdir()
            (root / "a.txt").write_bytes(b"alpha\n")
            executable = root / "dir/run"
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o755)
            index = "\0".join(
                (
                    "100755 " + "2" * 40 + " 0\tdir/run",
                    "100644 " + "1" * 40 + " 0\ta.txt",
                    "",
                )
            )
            self.assertEqual(
                tracked_tree_sha256(root, index, ""),
                "cf80821aa9d3c946e27b6836e121639f519a875704abf43e41a6239a3ef910af",
            )

    def test_aur_tree_digest_rejects_dirty_or_unsupported_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "file").write_text("content", encoding="utf-8")
            regular = "100644 " + "1" * 40 + " 0\tfile\0"
            with self.assertRaisesRegex(ValidationError, "not clean"):
                tracked_tree_sha256(root, regular, "?? untracked\0")
            symlink = "120000 " + "1" * 40 + " 0\tfile\0"
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                tracked_tree_sha256(root, symlink, "")
            submodule = "160000 " + "1" * 40 + " 0\tfile\0"
            with self.assertRaisesRegex(ValidationError, "unsupported"):
                tracked_tree_sha256(root, submodule, "")
            (root / "unexpected").mkdir()
            with self.assertRaisesRegex(ValidationError, "unexpected AUR directory"):
                tracked_tree_sha256(root, regular, "")

    def test_aur_srcinfo_identity_is_exact(self) -> None:
        self.assertEqual(
            srcinfo_identity("pkgbase = yay-bin\npkgname = yay-bin\n"),
            ("yay-bin", ("yay-bin",)),
        )
        self.assertNotEqual(
            srcinfo_identity("pkgbase = not-yay-bin\npkgname = yay-bin-debug\n"),
            (YAY_BIN_SOURCE.package_base, YAY_BIN_SOURCE.package_names),
        )

    @patch("ai_setup.packages.managers.os.geteuid", return_value=1000)
    def test_aur_refuses_unreviewed_checkout_before_makepkg(self, _: object) -> None:
        cases = (
            ("commit", "wrong\n", "origin\n", YAY_BIN_SOURCE.repository + "\n", "", None),
            (
                "remotes",
                YAY_BIN_SOURCE.commit + "\n",
                "origin\nextra\n",
                YAY_BIN_SOURCE.repository + "\n",
                "",
                None,
            ),
            (
                "origin",
                YAY_BIN_SOURCE.commit + "\n",
                "origin\n",
                "https://example.invalid/yay-bin.git\n",
                "",
                None,
            ),
            (
                "submodules",
                YAY_BIN_SOURCE.commit + "\n",
                "origin\n",
                YAY_BIN_SOURCE.repository + "\n",
                "-0123456789abcdef dependency\n",
                None,
            ),
            (
                "digest",
                YAY_BIN_SOURCE.commit + "\n",
                "origin\n",
                YAY_BIN_SOURCE.repository + "\n",
                "",
                "0" * 64,
            ),
        )
        for label, head, remotes, origin, submodules, digest in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                workspace = Path(raw)
                clone = workspace / "aur/yay-bin"
                config = workspace / "system-makepkg.conf"
                config.write_text("OPTIONS=(strip)\n", encoding="utf-8")
                responses: dict[tuple[str, ...], CommandResult] = {}
                values = {
                    ("git", "-C", str(clone), "rev-parse", "HEAD"): head,
                    ("git", "-C", str(clone), "remote"): remotes,
                    ("git", "-C", str(clone), "remote", "get-url", "origin"): origin,
                    (
                        "git",
                        "-C",
                        str(clone),
                        "submodule",
                        "status",
                        "--recursive",
                    ): submodules,
                    ("git", "-C", str(clone), "ls-files", "--stage", "-z"): "fixture",
                    (
                        "git",
                        "-C",
                        str(clone),
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                        "--ignored",
                        "-z",
                    ): "",
                }
                for argv, stdout in values.items():
                    responses[argv] = CommandResult(argv, 0, stdout, "")
                runner = FakeRunner(responses)
                tree_digest = digest or YAY_BIN_SOURCE.tree_sha256
                with (
                    patch(
                        "ai_setup.packages.managers.tracked_tree_sha256",
                        return_value=tree_digest,
                    ),
                    self.assertRaises(ValidationError),
                ):
                    AurManager(runner, workspace, config).bootstrap_yay()  # type: ignore[arg-type]
                self.assertFalse(any(command.argv[0] == "makepkg" for command in runner.commands))

    def test_aur_installs_only_requested_identifiers_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            system_config = workspace / "system-makepkg.conf"
            system_config.write_text("OPTIONS=(strip debug)\n", encoding="utf-8")
            runner = FakeRunner()
            AurManager(runner, workspace, system_config).install(  # type: ignore[arg-type]
                ("z-bin", "a-bin", "a-bin")
            )
            installs = [command for command in runner.commands if command.argv[:2] == ("yay", "-S")]
            self.assertEqual([command.argv[-1] for command in installs], ["a-bin", "z-bin"])
            self.assertTrue(all(len(command.failure_packages) == 1 for command in installs))
            self.assertFalse(
                any("-debug" in argument for command in installs for argument in command.argv)
            )
            self.assertTrue(all("--makepkgconf" in command.argv for command in installs))

    def test_flatpak_remote_idempotent(self) -> None:
        argv = ("flatpak", "remotes", "--user", "--columns=name")
        runner = FakeRunner({argv: CommandResult(argv, 0, "flathub\n", "")})
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            repository = home / ".local/share/flatpak/repo"
            repository.mkdir(parents=True)
            (repository / "config").write_text("[core]\nrepo_version=1\n")
            with patch("ai_setup.packages.managers.shutil.which", return_value="/usr/bin/flatpak"):
                FlatpakManager(runner, home).ensure_flathub()  # type: ignore[arg-type]
            self.assertFalse(any(c.argv[1] == "remote-add" for c in runner.commands))

    def test_flatpak_installs_steam_per_user_from_flathub_once(self) -> None:
        runner = FakeRunner()
        FlatpakManager(runner).install(("com.valvesoftware.Steam", "com.valvesoftware.Steam"))  # type: ignore[arg-type]
        self.assertEqual(
            [command.argv for command in runner.commands],
            [
                (
                    "flatpak",
                    "install",
                    "--user",
                    "--noninteractive",
                    "--or-update",
                    "flathub",
                    "com.valvesoftware.Steam",
                )
            ],
        )
