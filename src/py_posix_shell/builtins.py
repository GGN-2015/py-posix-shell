"""Builtin utilities."""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Callable, TextIO

from .errors import ShellBreak, ShellContinue, ShellExit, ShellReturn
from .lexer import is_name

Builtin = Callable[[object, list[str], TextIO, TextIO, TextIO], int]


SPECIAL_BUILTINS = {
    ":",
    ".",
    "break",
    "continue",
    "eval",
    "exec",
    "exit",
    "export",
    "readonly",
    "return",
    "set",
    "shift",
    "times",
    "trap",
    "unset",
}


def builtin_colon(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    return 0


def builtin_true(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    return 0


def builtin_false(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    return 1


def builtin_alias(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        for name in sorted(shell.aliases):
            stdout.write(f"alias {name}={quote_for_display(shell.aliases[name])}\n")
        return 0

    status = 0
    for arg in argv[1:]:
        if "=" not in arg:
            if arg in shell.aliases:
                stdout.write(f"alias {arg}={quote_for_display(shell.aliases[arg])}\n")
            else:
                stderr.write(f"alias: {arg}: not found\n")
                status = 1
            continue
        name, value = arg.split("=", 1)
        if not name:
            stderr.write("alias: empty alias name\n")
            status = 1
            continue
        shell.aliases[name] = value
    return status


def builtin_unalias(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        stderr.write("unalias: missing name\n")
        return 2
    if argv[1] == "-a":
        shell.aliases.clear()
        return 0
    status = 0
    for name in argv[1:]:
        if name in shell.aliases:
            del shell.aliases[name]
        else:
            stderr.write(f"unalias: {name}: not found\n")
            status = 1
    return status


def builtin_dot(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) < 2:
        stderr.write(".: missing filename\n")
        return 2
    path = argv[1]
    if not any(sep in path for sep in ("/", "\\")):
        found = shell.which(path)
        if found:
            path = found
    try:
        with open(path, "r", encoding="utf-8") as file:
            source = file.read()
    except OSError as exc:
        stderr.write(f".: {argv[1]}: {exc.strerror or exc}\n")
        return 1

    old_positional = shell.positional
    if len(argv) > 2:
        shell.positional = argv[2:]
    try:
        return shell.execute(source, allow_return=True)
    except ShellReturn as exc:
        return exc.status
    finally:
        shell.positional = old_positional


def builtin_eval(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        return 0
    return shell.execute(" ".join(argv[1:]), allow_return=True)


def builtin_exec(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        return 0
    status = shell.run_external(argv[1:], stdin=stdin, stdout=stdout, stderr=stderr)
    raise ShellExit(status)


def builtin_trap(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1 or argv[1] == "-p":
        for sig in sorted(shell.traps):
            stdout.write(f"trap -- {quote_for_display(shell.traps[sig])} {sig}\n")
        return 0
    if argv[1] == "-l":
        names = sorted(name for name in dir(signal) if name.startswith("SIG") and "_" not in name)
        stdout.write(" ".join(names) + "\n")
        return 0

    action = argv[1]
    status = 0
    for spec in argv[2:]:
        name = normalize_signal_name(spec)
        if not name:
            stderr.write(f"trap: bad signal {spec}\n")
            status = 1
            continue
        if action in {"-", ""}:
            shell.traps.pop(name, None)
            install_signal_handler(shell, name, None, stderr)
        else:
            shell.traps[name] = action
            install_signal_handler(shell, name, action, stderr)
    return status


def builtin_umask(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) > 2:
        stderr.write("umask: too many arguments\n")
        return 2
    old = os.umask(0)
    os.umask(old)
    if len(argv) == 1:
        stdout.write(f"{old:04o}\n")
        return 0
    try:
        new_mask = int(argv[1], 8)
    except ValueError:
        stderr.write(f"umask: {argv[1]}: invalid mask\n")
        return 2
    os.umask(new_mask)
    return 0


def builtin_times(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) > 1:
        stderr.write("times: too many arguments\n")
        return 2
    values = os.times()
    stdout.write(f"{format_time(values.user)} {format_time(values.system)}\n")
    stdout.write(f"{format_time(values.children_user)} {format_time(values.children_system)}\n")
    return 0


def builtin_hash(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    if args == ["-r"]:
        shell.command_hash.clear()
        return 0
    if not args:
        for name in sorted(shell.command_hash):
            stdout.write(f"{name}={shell.command_hash[name]}\n")
        return 0

    status = 0
    for name in args:
        if name == "-r":
            shell.command_hash.clear()
            continue
        path = shell.which(name)
        if path:
            shell.command_hash[name] = path
        else:
            stderr.write(f"hash: {name}: not found\n")
            status = 1
    return status


def builtin_history(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    show_count: int | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-c":
            shell.history.clear()
            return 0
        if arg == "-d":
            index += 1
            if index >= len(args):
                stderr.write("history: -d: option requires an argument\n")
                return 2
            try:
                offset = int(args[index])
            except ValueError:
                stderr.write(f"history: {args[index]}: numeric argument required\n")
                return 2
            if offset < 1 or offset > len(shell.history):
                stderr.write(f"history: {offset}: history position out of range\n")
                return 1
            del shell.history[offset - 1]
        elif arg.startswith("-"):
            stderr.write(f"history: invalid option: {arg}\n")
            return 2
        else:
            try:
                show_count = int(arg)
            except ValueError:
                stderr.write(f"history: {arg}: numeric argument required\n")
                return 2
        index += 1

    entries = shell.history[-show_count:] if show_count is not None else shell.history
    start = len(shell.history) - len(entries) + 1
    for number, entry in enumerate(entries, start):
        stdout.write(f"{number:5d}  {entry}\n")
    return 0


def builtin_echo(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    newline = True
    if args and args[0] == "-n":
        newline = False
        args = args[1:]
    stdout.write(" ".join(args))
    if newline:
        stdout.write("\n")
    return 0


def builtin_pwd(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    stdout.write(os.getcwd() + "\n")
    return 0


def builtin_cd(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) > 2:
        stderr.write("cd: too many arguments\n")
        return 2
    target = argv[1] if len(argv) == 2 else shell.get_parameter("HOME")
    if not target:
        stderr.write("cd: HOME not set\n")
        return 1
    print_new_path = False
    if target == "-":
        target = shell.get_parameter("OLDPWD")
        if not target:
            stderr.write("cd: OLDPWD not set\n")
            return 1
        print_new_path = True
    oldpwd = os.getcwd()
    try:
        os.chdir(os.path.expanduser(target))
    except OSError as exc:
        stderr.write(f"cd: {target}: {exc.strerror or exc}\n")
        return 1
    newpwd = os.getcwd()
    shell.set_parameter("OLDPWD", oldpwd, export=True)
    shell.set_parameter("PWD", newpwd, export=True)
    if print_new_path:
        stdout.write(newpwd + "\n")
    return 0


def builtin_exit(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) > 2:
        stderr.write("exit: too many arguments\n")
        return 2
    if len(argv) == 1:
        raise ShellExit(shell.last_status)
    try:
        status = int(argv[1], 10) & 0xFF
    except ValueError:
        stderr.write(f"exit: {argv[1]}: numeric argument required\n")
        raise ShellExit(2)
    raise ShellExit(status)


def builtin_return(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) > 2:
        stderr.write("return: too many arguments\n")
        return 2
    if len(argv) == 1:
        raise ShellReturn(shell.last_status)
    try:
        status = int(argv[1], 10) & 0xFF
    except ValueError:
        stderr.write(f"return: {argv[1]}: numeric argument required\n")
        raise ShellReturn(2)
    raise ShellReturn(status)


def builtin_break(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    count = parse_loop_count("break", argv, stderr)
    if count is None:
        return 2
    raise ShellBreak(count)


def builtin_continue(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    count = parse_loop_count("continue", argv, stderr)
    if count is None:
        return 2
    raise ShellContinue(count)


def builtin_export(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1 or argv[1:] == ["-p"]:
        for name in sorted(shell.env):
            stdout.write(f"export {name}={quote_for_display(shell.env[name])}\n")
        return 0

    status = 0
    for arg in argv[1:]:
        if arg == "-p":
            continue
        if "=" in arg:
            name, value = arg.split("=", 1)
        else:
            name = arg
            value = shell.get_parameter(name)
        if not is_name(name):
            stderr.write(f"export: {arg}: not a valid identifier\n")
            status = 1
            continue
        shell.set_parameter(name, value, export=True)
    return status


def builtin_unset(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    mode = "variable"
    items: list[tuple[str, str]] = []
    for arg in argv[1:]:
        if arg == "--":
            continue
        if arg == "-f":
            mode = "function"
            continue
        if arg == "-v":
            mode = "variable"
            continue
        if arg.startswith("-"):
            stderr.write(f"unset: invalid option: {arg}\n")
            return 2
        items.append((mode, arg))

    status = 0
    for mode, name in items:
        if not is_name(name):
            stderr.write(f"unset: {name}: not a valid identifier\n")
            status = 1
            continue
        if mode == "function":
            shell.functions.pop(name, None)
        else:
            shell.unset_parameter(name)
    return status


def builtin_readonly(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1 or argv[1:] == ["-p"]:
        for name in sorted(shell.readonly):
            stdout.write(f"readonly {name}={quote_for_display(shell.get_parameter(name))}\n")
        return 0
    status = 0
    for arg in argv[1:]:
        name = arg.split("=", 1)[0]
        if not is_name(name):
            stderr.write(f"readonly: {arg}: not a valid identifier\n")
            status = 1
            continue
        if "=" in arg:
            var_name, value = arg.split("=", 1)
            shell.set_parameter(var_name, value)
            shell.readonly.add(var_name)
        else:
            shell.readonly.add(name)
    return status


def builtin_set(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        merged = dict(shell.env)
        merged.update(shell.vars)
        for name in sorted(merged):
            stdout.write(f"{name}={quote_for_display(merged[name])}\n")
        return 0

    args = argv[1:]
    while args:
        arg = args[0]
        if arg == "--":
            shell.positional = args[1:]
            return 0
        if arg == "-":
            return 0
        if len(arg) > 1 and arg[0] in {"-", "+"}:
            enable = arg[0] == "-"
            for flag in arg[1:]:
                if flag == "e":
                    shell.options["errexit"] = enable
                elif flag == "u":
                    shell.options["nounset"] = enable
                elif flag == "f":
                    shell.options["noglob"] = enable
                else:
                    stderr.write(f"set: illegal option -- {flag}\n")
                    return 2
            args = args[1:]
            continue
        shell.positional = args
        return 0
    return 0


def builtin_shift(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) > 2:
        stderr.write("shift: too many arguments\n")
        return 2
    try:
        count = int(argv[1], 10) if len(argv) == 2 else 1
    except ValueError:
        stderr.write(f"shift: {argv[1]}: numeric argument required\n")
        return 2
    if count < 0 or count > len(shell.positional):
        stderr.write("shift: shift count out of range\n")
        return 1
    del shell.positional[:count]
    return 0


def builtin_getopts(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) < 3:
        stderr.write("getopts: usage: getopts optstring name [arg...]\n")
        return 2
    optstring = argv[1]
    name = argv[2]
    if not is_name(name):
        stderr.write(f"getopts: {name}: not a valid identifier\n")
        return 2

    args = argv[3:] if len(argv) > 3 else shell.positional
    optind = int(shell.get_parameter("OPTIND") or "1")
    silent = optstring.startswith(":")
    spec = optstring[1:] if silent else optstring

    if optind < 1:
        optind = 1
    if optind > len(args):
        shell.set_parameter(name, "?")
        return 1

    current = args[optind - 1]
    if current == "--":
        shell.set_parameter("OPTIND", str(optind + 1))
        shell.set_parameter(name, "?")
        shell._getopts_nextchar = 1
        return 1
    if not current.startswith("-") or current == "-":
        shell.set_parameter(name, "?")
        return 1

    pos = shell._getopts_nextchar
    if pos >= len(current):
        optind += 1
        pos = 1
        if optind > len(args):
            shell.set_parameter(name, "?")
            shell.set_parameter("OPTIND", str(optind))
            return 1
        current = args[optind - 1]
        if not current.startswith("-") or current == "-":
            shell.set_parameter(name, "?")
            shell.set_parameter("OPTIND", str(optind))
            return 1

    opt = current[pos]
    pos += 1
    needs_arg = f"{opt}:" in spec

    if opt not in spec.replace(":", ""):
        shell.set_parameter(name, "?")
        shell.set_parameter("OPTARG", opt)
        if not silent:
            stderr.write(f"getopts: illegal option -- {opt}\n")
        if pos >= len(current):
            optind += 1
            pos = 1
        shell._getopts_nextchar = pos
        shell.set_parameter("OPTIND", str(optind))
        return 0

    if needs_arg:
        if pos < len(current):
            optarg = current[pos:]
            optind += 1
            pos = 1
        elif optind < len(args):
            optarg = args[optind]
            optind += 2
            pos = 1
        else:
            shell.set_parameter(name, ":" if silent else "?")
            shell.set_parameter("OPTARG", opt)
            if not silent:
                stderr.write(f"getopts: option requires an argument -- {opt}\n")
            shell.set_parameter("OPTIND", str(optind))
            shell._getopts_nextchar = 1
            return 0
        shell.set_parameter("OPTARG", optarg)
    else:
        if pos >= len(current):
            optind += 1
            pos = 1
        shell.unset_parameter("OPTARG")

    shell.set_parameter(name, opt)
    shell.set_parameter("OPTIND", str(optind))
    shell._getopts_nextchar = pos
    return 0


def builtin_printf(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        return 0
    fmt = decode_escapes(argv[1])
    args = argv[2:] or [""]
    index = 0
    while index < len(args):
        wrote_conversion = False
        i = 0
        while i < len(fmt):
            char = fmt[i]
            if char != "%":
                stdout.write(char)
                i += 1
                continue
            if i + 1 < len(fmt) and fmt[i + 1] == "%":
                stdout.write("%")
                i += 2
                continue
            spec_start = i
            i += 1
            while i < len(fmt) and fmt[i] in "#0- +0123456789.":
                i += 1
            if i >= len(fmt):
                stdout.write(fmt[spec_start:])
                break
            spec = fmt[i]
            i += 1
            arg = args[index] if index < len(args) else ""
            index += 1
            wrote_conversion = True
            if spec == "s":
                stdout.write(arg)
            elif spec == "b":
                stdout.write(decode_escapes(arg))
            elif spec in "diu":
                try:
                    stdout.write(str(int(arg, 0)))
                except ValueError:
                    stdout.write("0")
            elif spec in "xX":
                try:
                    value = int(arg, 0)
                except ValueError:
                    value = 0
                stdout.write(format(value, spec))
            else:
                stdout.write("%" + spec)
        if not wrote_conversion:
            break
    return 0


def builtin_read(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    raw = False
    if args and args[0] == "-r":
        raw = True
        args = args[1:]
    if not args:
        args = ["REPLY"]
    line = stdin.readline()
    if line == "":
        return 1
    line = line.rstrip("\n")
    if not raw:
        line = line.replace("\\\n", "")
    values = line.split()
    for index, name in enumerate(args):
        if not is_name(name):
            stderr.write(f"read: {name}: not a valid identifier\n")
            return 2
        if index == len(args) - 1:
            value = " ".join(values[index:])
        else:
            value = values[index] if index < len(values) else ""
        shell.set_parameter(name, value)
    return 0


def builtin_type(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        stderr.write("type: missing operand\n")
        return 2
    status = 0
    for name in argv[1:]:
        if name in shell.functions:
            stdout.write(f"{name} is a shell function\n")
            continue
        if shell.is_builtin(name):
            stdout.write(f"{name} is a shell builtin\n")
            continue
        if shell.should_run_internal_utility(name, shell.env):
            stdout.write(f"{name} is a shell utility\n")
            continue
        path = shell.which(name)
        if path:
            stdout.write(f"{name} is {path}\n")
        else:
            stderr.write(f"type: {name}: not found\n")
            status = 1
    return status


def builtin_command(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    if not args:
        return 0
    if args[0] in {"-v", "-V"}:
        status = 0
        for name in args[1:]:
            if name in shell.functions:
                stdout.write(f"{name}\n" if args[0] == "-v" else f"{name} is a shell function\n")
                continue
            if shell.is_builtin(name):
                stdout.write(f"{name}\n" if args[0] == "-v" else f"{name} is a shell builtin\n")
                continue
            if shell.should_run_internal_utility(name, shell.env):
                stdout.write(f"{name}\n" if args[0] == "-v" else f"{name} is a shell utility\n")
                continue
            path = shell.which(name)
            if path:
                stdout.write(path + "\n")
            else:
                status = 1
        return status
    return shell.run_preexpanded(args, stdin=stdin, stdout=stdout, stderr=stderr)


def builtin_env(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    env = {} if args and args[0] == "-i" else dict(shell.env)
    if args and args[0] == "-i":
        args = args[1:]
    while args and "=" in args[0]:
        name, value = args[0].split("=", 1)
        env[name] = value
        args = args[1:]
    if not args:
        for name in sorted(env):
            stdout.write(f"{name}={env[name]}\n")
        return 0
    return shell.run_external(args, env=env, stdin=stdin, stdout=stdout, stderr=stderr)


def builtin_test(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = argv[1:]
    if argv[0] == "[":
        if not args or args[-1] != "]":
            stderr.write("[: missing closing ]\n")
            return 2
        args = args[:-1]
    return 0 if eval_test(args) else 1


def eval_test(args: list[str]) -> bool:
    if not args:
        return False
    if "!" in args:
        if args[0] == "!":
            return not eval_test(args[1:])
    if "-o" in args:
        index = args.index("-o")
        return eval_test(args[:index]) or eval_test(args[index + 1 :])
    if "-a" in args:
        index = args.index("-a")
        return eval_test(args[:index]) and eval_test(args[index + 1 :])
    if len(args) == 1:
        return args[0] != ""
    if len(args) == 2:
        op, value = args
        if op == "!":
            return not eval_test([value])
        if op == "-n":
            return value != ""
        if op == "-z":
            return value == ""
        if op == "-e":
            return os.path.exists(value)
        if op in {"-h", "-L"}:
            return os.path.islink(value)
        if op == "-f":
            return os.path.isfile(value)
        if op == "-d":
            return os.path.isdir(value)
        if op == "-r":
            return os.access(value, os.R_OK)
        if op == "-w":
            return os.access(value, os.W_OK)
        if op == "-x":
            return os.access(value, os.X_OK)
        if op == "-s":
            return os.path.exists(value) and os.path.getsize(value) > 0
        if op == "-t":
            try:
                return os.isatty(int(value))
            except (ValueError, OSError):
                return False
    if len(args) == 3:
        left, op, right = args
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        if op in {"-nt", "-ot", "-ef"}:
            if op == "-ef":
                try:
                    return os.path.samefile(left, right)
                except OSError:
                    return False
            if not os.path.exists(left) or not os.path.exists(right):
                return False
            left_mtime = os.path.getmtime(left)
            right_mtime = os.path.getmtime(right)
            return left_mtime > right_mtime if op == "-nt" else left_mtime < right_mtime
        if op in {"-eq", "-ne", "-gt", "-ge", "-lt", "-le"}:
            try:
                a = int(left, 10)
                b = int(right, 10)
            except ValueError:
                return False
            return {
                "-eq": a == b,
                "-ne": a != b,
                "-gt": a > b,
                "-ge": a >= b,
                "-lt": a < b,
                "-le": a <= b,
            }[op]
    return False


def normalize_signal_name(spec: str) -> str | None:
    if spec in {"0", "EXIT"}:
        return "EXIT"
    if spec.isdigit():
        number = int(spec)
        for name in dir(signal):
            if not name.startswith("SIG") or "_" in name:
                continue
            if getattr(signal, name) == number:
                return name
        return None
    name = spec.upper()
    if not name.startswith("SIG"):
        name = "SIG" + name
    return name if hasattr(signal, name) else None


def install_signal_handler(shell, name: str, action: str | None, stderr: TextIO) -> None:
    if name == "EXIT":
        return
    signum = getattr(signal, name, None)
    if signum is None:
        return
    try:
        if action is None:
            signal.signal(signum, signal.SIG_DFL)
        else:
            signal.signal(signum, lambda _sig, _frame: shell.execute(action, allow_return=True))
    except (OSError, RuntimeError, ValueError) as exc:
        stderr.write(f"trap: cannot trap {name}: {exc}\n")


def format_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    rest = seconds - (minutes * 60)
    return f"{minutes}m{rest:.2f}s"


def parse_loop_count(name: str, argv: list[str], stderr: TextIO) -> int | None:
    if len(argv) > 2:
        stderr.write(f"{name}: too many arguments\n")
        return None
    if len(argv) == 1:
        return 1
    try:
        count = int(argv[1], 10)
    except ValueError:
        stderr.write(f"{name}: {argv[1]}: numeric argument required\n")
        return None
    return max(1, count)


def quote_for_display(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def decode_escapes(value: str) -> str:
    result: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char != "\\":
            result.append(char)
            i += 1
            continue
        if i + 1 >= len(value):
            result.append("\\")
            break
        nxt = value[i + 1]
        mapping = {
            "a": "\a",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
            "\\": "\\",
        }
        if nxt in mapping:
            result.append(mapping[nxt])
            i += 2
            continue
        if nxt in "01234567":
            j = i + 1
            while j < len(value) and j < i + 4 and value[j] in "01234567":
                j += 1
            result.append(chr(int(value[i + 1 : j], 8)))
            i = j
            continue
        result.append(nxt)
        i += 2
    return "".join(result)


BUILTINS: dict[str, Builtin] = {
    ":": builtin_colon,
    ".": builtin_dot,
    "source": builtin_dot,
    "alias": builtin_alias,
    "unalias": builtin_unalias,
    "true": builtin_true,
    "false": builtin_false,
    "echo": builtin_echo,
    "pwd": builtin_pwd,
    "cd": builtin_cd,
    "eval": builtin_eval,
    "exec": builtin_exec,
    "trap": builtin_trap,
    "umask": builtin_umask,
    "times": builtin_times,
    "hash": builtin_hash,
    "history": builtin_history,
    "exit": builtin_exit,
    "return": builtin_return,
    "break": builtin_break,
    "continue": builtin_continue,
    "export": builtin_export,
    "readonly": builtin_readonly,
    "unset": builtin_unset,
    "set": builtin_set,
    "shift": builtin_shift,
    "getopts": builtin_getopts,
    "printf": builtin_printf,
    "read": builtin_read,
    "type": builtin_type,
    "command": builtin_command,
    "env": builtin_env,
    "test": builtin_test,
    "[": builtin_test,
}
