from __future__ import annotations

import argparse
import sys

from . import __version__
from .components import codex, git, github, precheck, shell, software, ssh
from .errors import AiError
from .runtime import Runtime

COMPONENTS = [("Software", software.reconcile), ("Git", git.reconcile),
              ("GitHub", github.reconcile), ("SSH", ssh.reconcile),
              ("Codex", codex.reconcile), ("Shell", shell.reconcile)]
SCOPED = {"git": ("Git", git.reconcile), "github": ("GitHub", github.reconcile),
          "ssh": ("SSH", ssh.reconcile)}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ai", description="Reconcile an Arch Linux workstation")
    result.add_argument("--version", action="version", version=f"ai {__version__}")
    result.add_argument("--dry-run", action="store_true", help="show needed changes without mutation")
    result.add_argument("--verbose", action="store_true", help="show commands and diagnostics")
    result.add_argument("component", nargs="?", choices=sorted(SCOPED))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    runtime = Runtime(dry_run=args.dry_run, verbose=args.verbose)
    try:
        precheck.reconcile(runtime)
        selected = [SCOPED[args.component]] if args.component else COMPONENTS
        for label, function in selected:
            before = len(runtime.changes)
            function(runtime)
            changes = runtime.changes[before:]
            if changes:
                print(f"{label}:")
                for change in changes:
                    print(f"  {change}")
            print(f"{label} ready.")
        if not args.component:
            print("\nWorkstation ready.")
        return 0
    except (AiError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
