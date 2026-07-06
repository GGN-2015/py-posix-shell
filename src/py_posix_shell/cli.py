"""Command-line interface."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .errors import ShellExit
from .posix_utils import utility_cygpath
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
    shell_path = sys.argv[0] or "pysh"
    if args.version:
        print(f"py-posix-shell {__version__}")
        return 0

    if args.command is not None:
        shell = Shell(argv0="pysh", shell_path=shell_path, positional=args.args)
        try:
            return shell.execute(args.command)
        except ShellExit as exc:
            return exc.status
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130

    if args.script:
        try:
            with open(args.script, "r", encoding="utf-8") as file:
                source = file.read()
        except OSError as exc:
            print(f"pysh: {args.script}: {exc}", file=sys.stderr)
            return 1
        shell = Shell(argv0=args.script, shell_path=shell_path, positional=args.args)
        try:
            return shell.execute(source)
        except ShellExit as exc:
            return exc.status
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130

    interactive = args.interactive or sys.stdin.isatty()
    shell = Shell(argv0="pysh", shell_path=shell_path, interactive=interactive)
    if interactive:
        try:
            shell.source_startup_file()
            return shell.repl()
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130
        except ShellExit as exc:
            return exc.status
    try:
        return shell.execute(sys.stdin.read())
    except ShellExit as exc:
        return exc.status
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def cygpath_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return utility_cygpath(None, ["cygpath", *args], sys.stdin, sys.stdout, sys.stderr)
