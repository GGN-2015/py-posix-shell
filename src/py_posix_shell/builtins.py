"""Builtin utilities."""

from __future__ import annotations

import os
import sys
from typing import Callable, TextIO

from .errors import ShellExit
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
    status = 0
    for name in argv[1:]:
        if not is_name(name):
            stderr.write(f"unset: {name}: not a valid identifier\n")
            status = 1
            continue
        shell.unset_parameter(name)
    return status


def builtin_set(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) == 1:
        merged = dict(shell.env)
        merged.update(shell.vars)
        for name in sorted(merged):
            stdout.write(f"{name}={quote_for_display(merged[name])}\n")
        return 0
    if argv[1] == "--":
        shell.positional = argv[2:]
        return 0
    stderr.write("set: only 'set' and 'set -- args...' are implemented\n")
    return 2


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
        if shell.is_builtin(name):
            stdout.write(f"{name} is a shell builtin\n")
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
            if shell.is_builtin(name):
                stdout.write(f"{name}\n" if args[0] == "-v" else f"{name} is a shell builtin\n")
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
        if op == "-f":
            return os.path.isfile(value)
        if op == "-d":
            return os.path.isdir(value)
    if len(args) == 3:
        left, op, right = args
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
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
    "true": builtin_true,
    "false": builtin_false,
    "echo": builtin_echo,
    "pwd": builtin_pwd,
    "cd": builtin_cd,
    "exit": builtin_exit,
    "export": builtin_export,
    "unset": builtin_unset,
    "set": builtin_set,
    "shift": builtin_shift,
    "printf": builtin_printf,
    "read": builtin_read,
    "type": builtin_type,
    "command": builtin_command,
    "env": builtin_env,
    "test": builtin_test,
    "[": builtin_test,
}

