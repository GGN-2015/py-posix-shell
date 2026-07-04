"""Command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .errors import ShellExit
from .shell import Shell


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pysh",
        description="Run a small cross-platform POSIX-style shell.",
    )
    parser.add_argument("-c", dest="command", help="read commands from COMMAND")
    parser.add_argument("-i", dest="interactive", action="store_true", help="force interactive mode")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("script", nargs="?", help="shell script to execute")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the script")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"py-posix-shell {__version__}")
        return 0

    if args.command is not None:
        shell = Shell(argv0="pysh", positional=args.args)
        try:
            return shell.execute(args.command)
        except ShellExit as exc:
            return exc.status

    if args.script:
        try:
            with open(args.script, "r", encoding="utf-8") as file:
                source = file.read()
        except OSError as exc:
            print(f"pysh: {args.script}: {exc}", file=sys.stderr)
            return 1
        shell = Shell(argv0=args.script, positional=args.args)
        try:
            return shell.execute(source)
        except ShellExit as exc:
            return exc.status

    interactive = args.interactive or sys.stdin.isatty()
    shell = Shell(argv0="pysh", interactive=interactive)
    if interactive:
        return shell.repl()
    try:
        return shell.execute(sys.stdin.read())
    except ShellExit as exc:
        return exc.status

