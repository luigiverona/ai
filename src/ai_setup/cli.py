from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_setup.catalog.loader import load_catalog
from ai_setup.errors import ApplicationError
from ai_setup.identity import IDENTITY
from ai_setup.models import Capability, Catalog, RunOptions, Selection
from ai_setup.planning.planner import build_plan
from ai_setup.status import StatusWorkflow
from ai_setup.ui.terminal import Terminal
from ai_setup.version import resolve_version
from ai_setup.workflow import Workflow


class InvocationKind(StrEnum):
    SETUP = "setup"
    PARTIAL = "partial"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class Invocation:
    kind: InvocationKind
    selection: Selection | None


class HelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, width=88, max_help_position=24)

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            lines = []
            for item in action._choices_actions:
                label = "apps [CATEGORY ...]" if item.dest == "apps" else item.dest
                lines.append(f"  {label:<22}{item.help}\n")
            return "".join(lines)
        return super()._format_action(action)


def _add_options(
    target: argparse.ArgumentParser | argparse._ArgumentGroup, *, root: bool = False
) -> None:
    default = None if root else argparse.SUPPRESS
    target.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=default,
        help="Show what would happen without making changes.",
    )
    target.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=default,
        help="Accept safe default confirmations.",
    )
    target.add_argument(
        "-v", "--verbose", action="store_true", default=default, help="Show detailed operations."
    )
    target.add_argument("--keep-temp", action="store_true", default=default, help=argparse.SUPPRESS)


def parser(catalog: Catalog | None = None) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog=IDENTITY.command_name,
        usage="%(prog)s [command] [options]",
        description=(
            "Personal setup for a fresh Arch Linux x86-64 workstation. "
            "Other distributions and customized installation environments are unsupported."
        ),
        formatter_class=HelpFormatter,
        add_help=False,
    )
    commands = result.add_subparsers(dest="command", title="commands", metavar="command")

    specs = (
        ("setup", "Set up the complete workstation."),
        ("apps", "Install all apps or selected categories."),
        ("git", "Configure the Git identity."),
        ("github", "Configure GitHub access."),
        ("ssh", "Configure GitHub SSH access."),
        ("codex", "Configure both Codex profiles."),
        ("status", "Check whether the workstation is ready."),
    )
    children: dict[str, argparse.ArgumentParser] = {}
    for name, description in specs:
        child = commands.add_parser(
            name,
            usage=f"{IDENTITY.command_name} {name} [options]",
            help=description,
            description=description,
            formatter_class=HelpFormatter,
            add_help=False,
        )
        child.add_argument("-h", "--help", action="help", help="Show this help message and exit.")
        _add_options(child)
        children[name] = child
    categories = (
        ", ".join(sorted(catalog.app_categories))
        if catalog
        else "loaded from the application catalog"
    )
    children["apps"].usage = f"{IDENTITY.command_name} apps [CATEGORY ...] [options]"
    children["apps"].add_argument(
        "categories",
        nargs="*",
        metavar="CATEGORY",
        help=f"Application categories to combine; omit to select all. Available: {categories}.",
    )
    options = result.add_argument_group("options")
    options.add_argument("-h", "--help", action="help", help="Show this help message and exit.")
    _add_options(options, root=True)
    options.add_argument(
        "--version",
        action="version",
        version=f"{IDENTITY.display_name} {resolve_version()}",
        help="Show the installed version and exit.",
    )
    return result


def invocation_from_args(args: argparse.Namespace, catalog: Catalog) -> Invocation:
    command = args.command
    if command in {None, "setup"}:
        return Invocation(InvocationKind.SETUP, Selection(frozenset(Capability), complete=True))
    if command == "status":
        return Invocation(InvocationKind.STATUS, None)
    mapping = {
        "apps": Capability.APPS,
        "git": Capability.GIT,
        "github": Capability.GITHUB,
        "ssh": Capability.SSH,
        "codex": Capability.CODEX,
    }
    categories = frozenset(getattr(args, "categories", ()))
    unknown = sorted(categories - catalog.app_categories)
    if unknown:
        valid = ", ".join(sorted(catalog.app_categories))
        raise ValueError(f"unknown application category '{unknown[0]}'; choose from: {valid}")
    return Invocation(
        InvocationKind.PARTIAL,
        Selection(frozenset({mapping[command]}), categories if command == "apps" else frozenset()),
    )


def _bool(args: argparse.Namespace, name: str) -> bool:
    return bool(getattr(args, name, False))


def main(argv: list[str] | None = None) -> int:
    terminal = Terminal()
    values = list(sys.argv[1:] if argv is None else argv)
    catalog = load_catalog()
    cli_parser = parser(catalog)
    try:
        args = cli_parser.parse_args(values)
        invocation = invocation_from_args(args, catalog)
        options = RunOptions(
            _bool(args, "dry_run"),
            _bool(args, "yes"),
            _bool(args, "verbose"),
            _bool(args, "keep_temp"),
            Path.home(),
        )
        if invocation.kind is InvocationKind.STATUS:
            return StatusWorkflow(catalog, options, terminal).run()
        if invocation.selection is None:
            raise RuntimeError("setup invocation requires a selection")
        plan = build_plan(invocation.selection, catalog)
        return Workflow(plan, options, terminal).run()
    except ValueError as exc:
        cli_parser.error(str(exc))
    except ApplicationError as exc:
        terminal.error(exc.component, exc.operation, exc.reason, exc.log_path, exc.packages)
        return exc.exit_code
    except KeyboardInterrupt:
        terminal.output("Setup paused.")
        terminal.output(f"Run {IDENTITY.command_name} again to continue.")
        return 130
    return 2
