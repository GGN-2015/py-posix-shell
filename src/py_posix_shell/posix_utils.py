"""Small POSIX utility fallbacks used when host commands are unavailable."""

from __future__ import annotations

import ast
import base64
import binascii
import datetime as _datetime
import difflib
import fnmatch
import getpass
import os
import platform
import re
import shutil
import signal
import shlex
import stat
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

try:  # pragma: no cover - platform dependent
    import grp
except ImportError:  # pragma: no cover - Windows
    grp = None  # type: ignore[assignment]

try:  # pragma: no cover - platform dependent
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None  # type: ignore[assignment]


Utility = Callable[[object, list[str], TextIO, TextIO, TextIO], int]


def utility_cat(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    status = 0
    paths = [arg for arg in argv[1:] if arg != "--"] or ["-"]
    for path in paths:
        try:
            if path == "-":
                stdout.write(stdin.read())
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as file:
                    shutil.copyfileobj(file, stdout)
        except OSError as exc:
            stderr.write(f"cat: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_base64(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    decode = False
    paths: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            paths.extend(argv[i + 1 :])
            break
        if arg in {"-d", "-D", "--decode"}:
            decode = True
        elif arg in {"-w", "--wrap"}:
            i += 1
            if i >= len(argv):
                stderr.write("base64: option requires an argument -- w\n")
                return 2
        elif arg.startswith("-w") and len(arg) > 2:
            pass
        elif arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag in {"d", "D"}:
                    decode = True
                else:
                    stderr.write(f"base64: invalid option -- {flag}\n")
                    return 2
        else:
            paths.append(arg)
        i += 1

    try:
        data = b"".join(read_binary_inputs(paths or ["-"], stdin))
        if decode:
            decoded = base64.b64decode(b"".join(data.split()), validate=False)
            write_binary_output(stdout, decoded)
        else:
            encoded = base64.encodebytes(data)
            write_binary_output(stdout, encoded)
    except (binascii.Error, ValueError) as exc:
        stderr.write(f"base64: invalid input: {exc}\n")
        return 1
    except OSError as exc:
        stderr.write(f"base64: {exc.strerror or exc}\n")
        return 1
    return 0


def utility_tr(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    delete = False
    squeeze = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg == "--":
            continue
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "d":
                    delete = True
                elif flag == "s":
                    squeeze = True
                else:
                    stderr.write(f"tr: invalid option -- {flag}\n")
                    return 2
        else:
            args.append(arg)
    if delete:
        if not args:
            stderr.write("tr: missing operand\n")
            return 2
        delete_chars = set(expand_tr_set(args[0]))
        squeeze_chars = set(expand_tr_set(args[1])) if squeeze and len(args) > 1 else delete_chars
        result = "".join(char for char in stdin.read() if char not in delete_chars)
        stdout.write(squeeze_repeated_chars(result, squeeze_chars) if squeeze else result)
        return 0
    if len(args) < 2:
        stderr.write("tr: missing operand after set1\n")
        return 2
    source = expand_tr_set(args[0])
    target = expand_tr_set(args[1])
    if not target:
        stderr.write("tr: set2 must not be empty\n")
        return 2
    translation: dict[int, str] = {}
    for index, char in enumerate(source):
        translation[ord(char)] = target[index] if index < len(target) else target[-1]
    result = stdin.read().translate(translation)
    if squeeze:
        result = squeeze_repeated_chars(result, set(target))
    stdout.write(result)
    return 0


def utility_clear(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        if arg == "-T":
            index += 2
            continue
        if arg in {"-V", "--version"}:
            stdout.write("py-posix-shell clear fallback\n")
            return 0
        if arg in {"-x"}:
            index += 1
            continue
        if arg.startswith("-"):
            stderr.write(f"clear: invalid option: {arg}\n")
            return 2
        index += 1
    stdout.write("\033[H\033[2J")
    try:
        stdout.flush()
    except OSError:
        pass
    return 0


def utility_ls(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    show_all = False
    almost_all = False
    long_format = False
    one_per_line = False
    directory_itself = False
    paths: list[str] = []
    status = 0

    for arg in argv[1:]:
        if arg == "--":
            continue
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "a":
                    show_all = True
                elif flag == "A":
                    almost_all = True
                elif flag == "l":
                    long_format = True
                    one_per_line = True
                elif flag == "1":
                    one_per_line = True
                elif flag == "d":
                    directory_itself = True
                else:
                    stderr.write(f"ls: invalid option -- {flag}\n")
                    return 2
            continue
        paths.append(arg)

    paths = paths or ["."]
    multiple = len(paths) > 1
    for path_index, path in enumerate(paths):
        target = Path(path)
        try:
            if target.is_dir() and not directory_itself:
                entries = sorted(target.iterdir(), key=lambda item: item.name.lower())
                filtered = []
                for entry in entries:
                    if entry.name in {".", ".."} and not show_all:
                        continue
                    if entry.name.startswith(".") and not (show_all or almost_all):
                        continue
                    filtered.append(entry)
            else:
                filtered = [target]
            if multiple:
                if path_index:
                    stdout.write("\n")
                stdout.write(f"{path}:\n")
            if long_format:
                for entry in filtered:
                    stdout.write(format_long_listing(entry, display_name(entry, target)) + "\n")
            elif one_per_line or multiple:
                for entry in filtered:
                    stdout.write(display_name(entry, target) + "\n")
            else:
                stdout.write("  ".join(display_name(entry, target) for entry in filtered))
                if filtered:
                    stdout.write("\n")
        except OSError as exc:
            stderr.write(f"ls: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_cp(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    recursive = False
    force = False
    preserve = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag in {"R", "r"}:
                    recursive = True
                elif flag == "f":
                    force = True
                elif flag == "p":
                    preserve = True
                else:
                    stderr.write(f"cp: invalid option -- {flag}\n")
                    return 2
            continue
        args.append(arg)
    if len(args) < 2:
        stderr.write("cp: missing file operand\n")
        return 2

    dest = Path(args[-1])
    sources = [Path(arg) for arg in args[:-1]]
    if len(sources) > 1 and not dest.is_dir():
        stderr.write(f"cp: target '{dest}' is not a directory\n")
        return 1

    status = 0
    for source in sources:
        try:
            target = dest / source.name if dest.is_dir() else dest
            if source.is_dir():
                if not recursive:
                    stderr.write(f"cp: -R not specified; omitting directory '{source}'\n")
                    status = 1
                    continue
                if target.exists() and force and target.is_file():
                    target.unlink()
                shutil.copytree(source, target, dirs_exist_ok=True, copy_function=shutil.copy2 if preserve else shutil.copy)
            else:
                if target.exists() and target.is_dir():
                    target = target / source.name
                if force and target.exists() and not target.is_dir():
                    target.unlink()
                copy = shutil.copy2 if preserve else shutil.copy
                copy(source, target)
        except OSError as exc:
            stderr.write(f"cp: {source}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_mv(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = [arg for arg in argv[1:] if arg not in {"-f", "--"}]
    if len(args) < 2:
        stderr.write("mv: missing file operand\n")
        return 2
    dest = Path(args[-1])
    sources = [Path(arg) for arg in args[:-1]]
    if len(sources) > 1 and not dest.is_dir():
        stderr.write(f"mv: target '{dest}' is not a directory\n")
        return 1
    status = 0
    for source in sources:
        try:
            shutil.move(str(source), str(dest / source.name if dest.is_dir() else dest))
        except OSError as exc:
            stderr.write(f"mv: {source}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_rm(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    recursive = False
    force = False
    paths: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag in {"r", "R"}:
                    recursive = True
                elif flag == "f":
                    force = True
                else:
                    stderr.write(f"rm: invalid option -- {flag}\n")
                    return 2
            continue
        paths.append(arg)
    if not paths and not force:
        stderr.write("rm: missing operand\n")
        return 2
    status = 0
    for path_text in paths:
        path = Path(path_text)
        try:
            if path.is_dir() and not path.is_symlink():
                if not recursive:
                    stderr.write(f"rm: cannot remove '{path}': Is a directory\n")
                    status = 1
                    continue
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            if not force:
                stderr.write(f"rm: cannot remove '{path}': No such file or directory\n")
                status = 1
        except OSError as exc:
            stderr.write(f"rm: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_mkdir(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    parents = False
    paths: list[str] = []
    for arg in argv[1:]:
        if arg == "-p":
            parents = True
        elif arg == "--":
            continue
        else:
            paths.append(arg)
    if not paths:
        stderr.write("mkdir: missing operand\n")
        return 2
    status = 0
    for path in paths:
        try:
            Path(path).mkdir(parents=parents, exist_ok=parents)
        except OSError as exc:
            stderr.write(f"mkdir: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_rmdir(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    paths = [arg for arg in argv[1:] if arg != "--"]
    if not paths:
        stderr.write("rmdir: missing operand\n")
        return 2
    status = 0
    for path in paths:
        try:
            Path(path).rmdir()
        except OSError as exc:
            stderr.write(f"rmdir: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_touch(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    no_create = False
    paths: list[str] = []
    for arg in argv[1:]:
        if arg == "-c":
            no_create = True
        elif arg == "--":
            continue
        else:
            paths.append(arg)
    if not paths:
        stderr.write("touch: missing file operand\n")
        return 2
    status = 0
    for path_text in paths:
        path = Path(path_text)
        try:
            if path.exists():
                os.utime(path, None)
            elif not no_create:
                path.touch()
        except OSError as exc:
            stderr.write(f"touch: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_ps(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    stdout.write("  PID COMMAND\n")
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            completed = None
        if completed and completed.returncode == 0:
            for line in completed.stdout.splitlines()[:200]:
                parts = parse_csv_line(line)
                if len(parts) >= 2:
                    stdout.write(f"{parts[1]:>5} {parts[0]}\n")
            return 0
    stdout.write(f"{os.getpid():>5} {Path(sys_argv0()).name or 'python'}\n")
    return 0


def utility_sleep(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) < 2:
        stderr.write("sleep: missing operand\n")
        return 2
    try:
        seconds = sum(float(arg) for arg in argv[1:])
    except ValueError:
        stderr.write("sleep: invalid time interval\n")
        return 2
    time.sleep(max(0.0, seconds))
    return 0


def utility_basename(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) < 2:
        stderr.write("basename: missing operand\n")
        return 2
    name = os.path.basename(argv[1].rstrip("/\\"))
    if len(argv) > 2 and name.endswith(argv[2]):
        name = name[: -len(argv[2])]
    stdout.write(name + "\n")
    return 0


def utility_dirname(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) < 2:
        stderr.write("dirname: missing operand\n")
        return 2
    dirname = os.path.dirname(argv[1].rstrip("/\\")) or "."
    stdout.write(dirname + "\n")
    return 0


def utility_head(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    count, paths, status = parse_line_count_args("head", argv, stderr, default=10)
    if status:
        return status
    return write_selected_lines(paths or ["-"], stdin, stdout, stderr, lambda lines: lines[:count], "head")


def utility_tail(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    count, paths, status = parse_line_count_args("tail", argv, stderr, default=10)
    if status:
        return status
    return write_selected_lines(paths or ["-"], stdin, stdout, stderr, lambda lines: lines[-count:], "tail")


def utility_wc(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    count_lines = count_words = count_bytes = False
    paths: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "l":
                    count_lines = True
                elif flag == "w":
                    count_words = True
                elif flag in {"c", "m"}:
                    count_bytes = True
                else:
                    stderr.write(f"wc: invalid option -- {flag}\n")
                    return 2
        else:
            paths.append(arg)
    if not (count_lines or count_words or count_bytes):
        count_lines = count_words = count_bytes = True

    rows: list[tuple[int, int, int, str | None]] = []
    status = 0
    for path in paths or ["-"]:
        try:
            data = next(iter(read_binary_inputs([path], stdin)))
        except OSError as exc:
            stderr.write(f"wc: {path}: {exc.strerror or exc}\n")
            status = 1
            continue
        text = data.decode("utf-8", errors="replace")
        rows.append((data.count(b"\n"), len(text.split()), len(data), None if path == "-" else path))
    for lines, words, bytes_count, name in rows:
        values: list[str] = []
        if count_lines:
            values.append(str(lines))
        if count_words:
            values.append(str(words))
        if count_bytes:
            values.append(str(bytes_count))
        stdout.write(" ".join(values))
        if name:
            stdout.write(f" {name}")
        stdout.write("\n")
    return status


def utility_grep(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    command = Path(argv[0]).name
    fixed = command == "fgrep"
    ignore_case = False
    line_numbers = False
    invert = False
    quiet = False
    count_only = False
    list_files = False
    with_filename: bool | None = None
    patterns: list[str] = []
    args: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            args.extend(argv[i + 1 :])
            break
        if arg in {"-e", "-f"}:
            i += 1
            if i >= len(argv):
                stderr.write(f"grep: option requires an argument -- {arg[1:]}\n")
                return 2
            if arg == "-e":
                patterns.append(argv[i])
            else:
                try:
                    patterns.extend(Path(argv[i]).read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError as exc:
                    stderr.write(f"grep: {argv[i]}: {exc.strerror or exc}\n")
                    return 2
        elif arg.startswith("-") and arg != "-":
            j = 1
            while j < len(arg):
                flag = arg[j]
                if flag == "i":
                    ignore_case = True
                elif flag == "n":
                    line_numbers = True
                elif flag == "v":
                    invert = True
                elif flag == "q":
                    quiet = True
                elif flag == "c":
                    count_only = True
                elif flag == "l":
                    list_files = True
                elif flag == "H":
                    with_filename = True
                elif flag == "h":
                    with_filename = False
                elif flag == "F":
                    fixed = True
                elif flag == "E":
                    fixed = False
                elif flag == "e":
                    patterns.append(arg[j + 1 :])
                    j = len(arg)
                    break
                else:
                    stderr.write(f"grep: invalid option -- {flag}\n")
                    return 2
                j += 1
        else:
            args.append(arg)
        i += 1
    if not patterns:
        if not args:
            stderr.write("grep: missing pattern\n")
            return 2
        patterns.append(args.pop(0))

    paths = args or ["-"]
    regexes: list[re.Pattern[str]] = []
    if not fixed:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regexes = [re.compile(pattern, flags) for pattern in patterns]
        except re.error as exc:
            stderr.write(f"grep: {exc}\n")
            return 2

    def line_matches(line: str) -> bool:
        haystack = line.lower() if ignore_case and fixed else line
        if fixed:
            needles = [pattern.lower() for pattern in patterns] if ignore_case else patterns
            return any(needle in haystack for needle in needles)
        return any(regex.search(line) is not None for regex in regexes)

    matched_any = False
    multiple = len(paths) > 1
    status = 0
    for path, text in read_text_inputs(paths, stdin, stderr, "grep"):
        if text is None:
            status = 2
            continue
        file_matches = 0
        should_prefix_file = with_filename if with_filename is not None else (multiple and path != "-")
        for number, line in enumerate(text.splitlines(keepends=True), 1):
            ok = line_matches(line.rstrip("\n"))
            if invert:
                ok = not ok
            if not ok:
                continue
            matched_any = True
            file_matches += 1
            if quiet:
                return 0
            if list_files:
                break
            if count_only:
                continue
            prefix = ""
            if should_prefix_file:
                prefix += f"{path}:"
            if line_numbers:
                prefix += f"{number}:"
            stdout.write(prefix + line)
            if not line.endswith("\n"):
                stdout.write("\n")
        if list_files and file_matches and path != "-":
            stdout.write(path + "\n")
        elif count_only:
            if should_prefix_file:
                stdout.write(f"{path}:")
            stdout.write(str(file_matches) + "\n")
    return 0 if matched_any else (status or 1)


def utility_chmod(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if len(argv) < 3:
        stderr.write("chmod: missing operand\n")
        return 2
    try:
        mode = int(argv[1], 8)
    except ValueError:
        stderr.write(f"chmod: invalid mode: {argv[1]}\n")
        return 2
    status = 0
    for path in argv[2:]:
        try:
            os.chmod(path, mode)
        except OSError as exc:
            stderr.write(f"chmod: {path}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_ln(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    symbolic = False
    force = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "s":
                    symbolic = True
                elif flag == "f":
                    force = True
                else:
                    stderr.write(f"ln: invalid option -- {flag}\n")
                    return 2
        else:
            args.append(arg)
    if len(args) != 2:
        stderr.write("ln: expected source and target\n")
        return 2
    source, target = args
    try:
        if force and os.path.lexists(target):
            os.unlink(target)
        if symbolic:
            os.symlink(source, target)
        else:
            os.link(source, target)
    except OSError as exc:
        stderr.write(f"ln: {target}: {exc.strerror or exc}\n")
        return 1
    return 0


def utility_date(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    now = _datetime.datetime.now()
    if len(argv) > 1 and argv[1].startswith("+"):
        stdout.write(now.strftime(argv[1][1:]) + "\n")
    else:
        stdout.write(now.ctime() + "\n")
    return 0


def utility_uname(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    flags = argv[1:] or ["-s"]
    if "-a" in flags:
        values = [platform.system(), platform.node(), platform.release(), platform.version(), platform.machine()]
    else:
        values = []
        for arg in flags:
            if arg == "-s":
                values.append(platform.system())
            elif arg == "-n":
                values.append(platform.node())
            elif arg == "-r":
                values.append(platform.release())
            elif arg == "-v":
                values.append(platform.version())
            elif arg == "-m":
                values.append(platform.machine())
            else:
                stderr.write(f"uname: invalid option: {arg}\n")
                return 2
    stdout.write(" ".join(values) + "\n")
    return 0


def utility_whoami(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    stdout.write(getpass.getuser() + "\n")
    return 0


def utility_id(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    user = getpass.getuser()
    stdout.write(f"uid={uid}({user}) gid={gid}({user})\n")
    return 0


def utility_kill(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    sig = signal.SIGTERM
    args = argv[1:]
    if args and args[0].startswith("-") and args[0] != "-":
        name = args[0][1:].upper()
        if name.isdigit():
            sig = int(name)
        else:
            if not name.startswith("SIG"):
                name = "SIG" + name
            sig = getattr(signal, name, signal.SIGTERM)
        args = args[1:]
    if not args:
        stderr.write("kill: missing pid\n")
        return 2
    status = 0
    for pid_text in args:
        try:
            os.kill(int(pid_text), sig)
        except (ValueError, OSError) as exc:
            stderr.write(f"kill: {pid_text}: {exc}\n")
            status = 1
    return status


def utility_echo(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    newline = True
    args = argv[1:]
    if args and args[0] == "-n":
        newline = False
        args = args[1:]
    stdout.write(" ".join(args))
    if newline:
        stdout.write("\n")
    return 0


def utility_printf(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    from .builtins import builtin_printf

    return builtin_printf(shell, argv, stdin, stdout, stderr)


def utility_pwd(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    stdout.write(os.getcwd() + "\n")
    return 0


def utility_yes(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    text = " ".join(argv[1:]) if len(argv) > 1 else "y"
    # Pipelines are string-buffered in this shell, so an infinite producer would
    # starve the consumer. This is high enough for ordinary "yes | head" use.
    limit = int(getattr(shell, "env", {}).get("PY_POSIX_SHELL_YES_LIMIT", "10000"))
    try:
        for _ in range(max(0, limit)):
            stdout.write(text + "\n")
            stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


def utility_unlink(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = [arg for arg in argv[1:] if arg != "--"]
    if len(args) != 1:
        stderr.write("unlink: missing operand\n" if not args else "unlink: extra operand\n")
        return 2
    try:
        Path(args[0]).unlink()
    except OSError as exc:
        stderr.write(f"unlink: {args[0]}: {exc.strerror or exc}\n")
        return 1
    return 0


def utility_install(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    mode: int | None = None
    directory = False
    create_dirs = False
    strip = False
    owner: str | None = None
    group: str | None = None
    args: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            args.extend(argv[i + 1 :])
            break
        if arg in {"-d", "-D", "-s"}:
            directory = directory or arg == "-d"
            create_dirs = create_dirs or arg == "-D"
            strip = strip or arg == "-s"
            i += 1
            continue
        if arg in {"-m", "-o", "-g"}:
            i += 1
            if i >= len(argv):
                stderr.write(f"install: option requires an argument -- {arg[1:]}\n")
                return 2
            if arg == "-m":
                try:
                    mode = int(argv[i], 8)
                except ValueError:
                    stderr.write(f"install: invalid mode: {argv[i]}\n")
                    return 2
            elif arg == "-o":
                owner = argv[i]
            else:
                group = argv[i]
            i += 1
            continue
        if arg.startswith("-"):
            stderr.write(f"install: invalid option: {arg}\n")
            return 2
        args.append(arg)
        i += 1

    if strip:
        pass
    if directory:
        if not args:
            stderr.write("install: missing operand\n")
            return 2
        status = 0
        for path_text in args:
            try:
                path = Path(path_text)
                path.mkdir(parents=True, exist_ok=True)
                if mode is not None:
                    os.chmod(path, mode)
                apply_owner_group(path, owner, group, stderr, "install")
            except OSError as exc:
                stderr.write(f"install: {path_text}: {exc.strerror or exc}\n")
                status = 1
        return status

    if len(args) < 2:
        stderr.write("install: missing file operand\n")
        return 2
    dest = Path(args[-1])
    sources = [Path(arg) for arg in args[:-1]]
    if len(sources) > 1 and not dest.is_dir():
        stderr.write(f"install: target '{dest}' is not a directory\n")
        return 1
    status = 0
    for source in sources:
        target = dest / source.name if dest.is_dir() else dest
        try:
            if create_dirs:
                target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            os.chmod(target, mode if mode is not None else 0o755)
            if apply_owner_group(target, owner, group, stderr, "install") != 0:
                status = 1
        except OSError as exc:
            stderr.write(f"install: {source}: {exc.strerror or exc}\n")
            status = 1
    return status


def utility_chown(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    recursive = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg in {"-R", "--recursive"}:
            recursive = True
        elif arg == "--":
            continue
        else:
            args.append(arg)
    if len(args) < 2:
        stderr.write("chown: missing operand\n")
        return 2
    owner_spec = args[0]
    owner, group = split_owner_group(owner_spec)
    status = 0
    for path_text in args[1:]:
        for path in walk_targets(Path(path_text), recursive):
            status |= apply_owner_group(path, owner, group, stderr, "chown")
    return 1 if status else 0


def utility_chgrp(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    recursive = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg in {"-R", "--recursive"}:
            recursive = True
        elif arg == "--":
            continue
        else:
            args.append(arg)
    if len(args) < 2:
        stderr.write("chgrp: missing operand\n")
        return 2
    status = 0
    for path_text in args[1:]:
        for path in walk_targets(Path(path_text), recursive):
            status |= apply_owner_group(path, None, args[0], stderr, "chgrp")
    return 1 if status else 0


def utility_sort(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    reverse = False
    unique = False
    numeric = False
    ignore_case = False
    output: str | None = None
    paths: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-o":
            i += 1
            if i >= len(argv):
                stderr.write("sort: option requires an argument -- o\n")
                return 2
            output = argv[i]
        elif arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "r":
                    reverse = True
                elif flag == "u":
                    unique = True
                elif flag == "n":
                    numeric = True
                elif flag == "f":
                    ignore_case = True
                else:
                    stderr.write(f"sort: invalid option -- {flag}\n")
                    return 2
        else:
            paths.append(arg)
        i += 1

    lines: list[str] = []
    status = 0
    for _path, text in read_text_inputs(paths or ["-"], stdin, stderr, "sort"):
        if text is None:
            status = 1
            continue
        lines.extend(text.splitlines(keepends=True))

    def key(line: str):
        value = line.rstrip("\n")
        if ignore_case:
            value = value.lower()
        if numeric:
            match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", value)
            return float(match.group(0)) if match else 0.0
        return value

    lines.sort(key=key, reverse=reverse)
    if unique:
        lines = unique_adjacent(lines, key)
    result = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    if output:
        Path(output).write_text(result, encoding="utf-8")
    else:
        stdout.write(result)
    return status


def utility_uniq(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    count = False
    duplicates_only = False
    unique_only = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "c":
                    count = True
                elif flag == "d":
                    duplicates_only = True
                elif flag == "u":
                    unique_only = True
                else:
                    stderr.write(f"uniq: invalid option -- {flag}\n")
                    return 2
        else:
            args.append(arg)
    input_path = args[0] if args else "-"
    output_path = args[1] if len(args) > 1 else None
    text = stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8", errors="replace")
    grouped: list[tuple[str, int]] = []
    for line in text.splitlines(keepends=True):
        if grouped and grouped[-1][0] == line:
            grouped[-1] = (line, grouped[-1][1] + 1)
        else:
            grouped.append((line, 1))
    out: list[str] = []
    for line, amount in grouped:
        if duplicates_only and amount == 1:
            continue
        if unique_only and amount != 1:
            continue
        prefix = f"{amount:7d} " if count else ""
        out.append(prefix + line)
    result = "".join(out)
    if output_path:
        Path(output_path).write_text(result, encoding="utf-8")
    else:
        stdout.write(result)
    return 0


def utility_cut(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    byte_ranges: list[tuple[int | None, int | None]] | None = None
    char_ranges: list[tuple[int | None, int | None]] | None = None
    fields: list[int] | None = None
    delimiter = "\t"
    paths: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"-b", "-c", "-f", "-d"}:
            i += 1
            if i >= len(argv):
                stderr.write(f"cut: option requires an argument -- {arg[1:]}\n")
                return 2
            if arg == "-b":
                byte_ranges = parse_ranges(argv[i])
            elif arg == "-c":
                char_ranges = parse_ranges(argv[i])
            elif arg == "-f":
                fields = [num for start, end in parse_ranges(argv[i]) for num in range(start or 1, (end or start or 1) + 1)]
            else:
                delimiter = argv[i]
        elif arg.startswith("-b"):
            byte_ranges = parse_ranges(arg[2:])
        elif arg.startswith("-c"):
            char_ranges = parse_ranges(arg[2:])
        elif arg.startswith("-f"):
            fields = [num for start, end in parse_ranges(arg[2:]) for num in range(start or 1, (end or start or 1) + 1)]
        elif arg.startswith("-d"):
            delimiter = arg[2:]
        else:
            paths.append(arg)
        i += 1
    if byte_ranges is None and char_ranges is None and fields is None:
        stderr.write("cut: you must specify a list of bytes, characters, or fields\n")
        return 2
    for _path, text in read_text_inputs(paths or ["-"], stdin, stderr, "cut"):
        if text is None:
            continue
        for line in text.splitlines():
            if fields is not None:
                parts = line.split(delimiter)
                stdout.write(delimiter.join(parts[index - 1] for index in fields if 1 <= index <= len(parts)) + "\n")
            else:
                ranges = char_ranges or byte_ranges or []
                stdout.write(select_ranges(line, ranges) + "\n")
    return 0


def utility_find(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    paths: list[str] = []
    expr: list[str] = []
    args = argv[1:] or ["."]
    expression_markers = {"-name", "-iname", "-type", "-maxdepth", "-mindepth", "-print", "-delete", "-exec"}
    for index, arg in enumerate(args):
        if arg in expression_markers or arg in {"!", "-not", "-o", "-or", "-a", "-and"}:
            expr = args[index:]
            break
        paths.append(arg)
    if not paths:
        paths = ["."]
    predicates = parse_find_expression(expr, stderr)
    if predicates is None:
        return 2
    status = 0
    for root_text in paths:
        root = Path(root_text)
        if not root.exists():
            stderr.write(f"find: '{root}': No such file or directory\n")
            status = 1
            continue
        for path in iter_find_paths(root, predicates.maxdepth):
            try:
                depth = len(path.resolve().relative_to(root.resolve()).parts) if path != root else 0
            except ValueError:
                depth = max(0, len(path.parts) - len(root.parts))
            if depth < predicates.mindepth:
                continue
            matched = find_matches(path, predicates)
            if not matched:
                continue
            if predicates.delete:
                try:
                    if path.is_dir() and not path.is_symlink():
                        path.rmdir()
                    else:
                        path.unlink()
                except OSError as exc:
                    stderr.write(f"find: cannot delete '{path}': {exc.strerror or exc}\n")
                    status = 1
                continue
            if predicates.exec_command:
                command = [str(path) if part == "{}" else part for part in predicates.exec_command]
                status |= shell.run_preexpanded(command, stdin=stdin, stdout=stdout, stderr=stderr)
            if predicates.print_result:
                stdout.write(str(path) + "\n")
    return status


def utility_xargs(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    null_split = False
    max_args = 500
    replace: str | None = None
    command: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"-0", "--null"}:
            null_split = True
        elif arg in {"-n", "--max-args"}:
            i += 1
            max_args = int(argv[i])
        elif arg.startswith("-n") and arg[2:].isdigit():
            max_args = int(arg[2:])
        elif arg in {"-I", "--replace"}:
            i += 1
            replace = argv[i]
        else:
            command = argv[i:]
            break
        i += 1
    data = stdin.read()
    items = data.split("\0") if null_split else shlex_split(data)
    items = [item for item in items if item != ""]
    if not command:
        command = ["echo"]
    if replace is not None:
        status = 0
        for item in items:
            expanded = [part.replace(replace, item) for part in command]
            status |= shell.run_preexpanded(expanded, stdin=stdin, stdout=stdout, stderr=stderr)
        return status
    status = 0
    for chunk in chunks(items, max_args):
        status |= shell.run_preexpanded([*command, *chunk], stdin=stdin, stdout=stdout, stderr=stderr)
    return status


def utility_updatedb(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    output = locate_db_path()
    roots = ["."]
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"-o", "--output"}:
            i += 1
            if i >= len(argv):
                stderr.write("updatedb: option requires an argument -- output\n")
                return 2
            output = Path(argv[i])
        elif arg in {"-U", "--database-root"}:
            i += 1
            if i >= len(argv):
                stderr.write("updatedb: option requires an argument -- database-root\n")
                return 2
            roots = [argv[i]]
        else:
            roots.append(arg)
        i += 1
    paths: list[str] = []
    for root in roots:
        for path, _dirs, files in os.walk(root):
            paths.append(str(Path(path)))
            paths.extend(str(Path(path) / file) for file in files)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sorted(set(paths))) + "\n", encoding="utf-8")
    return 0


def utility_locate(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    ignore_case = False
    limit: int | None = None
    database = locate_db_path()
    patterns: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"-i", "--ignore-case"}:
            ignore_case = True
        elif arg in {"-n", "--limit"}:
            i += 1
            limit = int(argv[i])
        elif arg in {"-d", "--database"}:
            i += 1
            database = Path(argv[i])
        else:
            patterns.append(arg)
        i += 1
    if not patterns:
        stderr.write("locate: no pattern to search for specified\n")
        return 2
    try:
        entries = database.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        stderr.write(f"locate: cannot open database '{database}': {exc.strerror or exc}\n")
        return 1
    count = 0
    for entry in entries:
        hay = entry.lower() if ignore_case else entry
        ok = any((pattern.lower() if ignore_case else pattern) in hay or fnmatch.fnmatchcase(hay, pattern.lower() if ignore_case else pattern) for pattern in patterns)
        if ok:
            stdout.write(entry + "\n")
            count += 1
            if limit is not None and count >= limit:
                break
    return 0 if count else 1


def utility_diff(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    unified = False
    ignore_case = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg in {"-u", "--unified"}:
            unified = True
        elif arg in {"-i", "--ignore-case"}:
            ignore_case = True
        elif arg.startswith("-"):
            stderr.write(f"diff: unsupported option: {arg}\n")
            return 2
        else:
            args.append(arg)
    if len(args) != 2:
        stderr.write("diff: missing operand\n")
        return 2
    left = read_file_or_stdin(args[0], stdin)
    right = read_file_or_stdin(args[1], stdin)
    compare_left = [line.lower() for line in left] if ignore_case else left
    compare_right = [line.lower() for line in right] if ignore_case else right
    if compare_left == compare_right:
        return 0
    if unified:
        stdout.writelines(difflib.unified_diff(left, right, fromfile=args[0], tofile=args[1]))
    else:
        stdout.writelines(difflib.ndiff(left, right))
    return 1


def utility_cmp(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    silent = False
    args: list[str] = []
    for arg in argv[1:]:
        if arg in {"-s", "--silent", "--quiet"}:
            silent = True
        else:
            args.append(arg)
    if len(args) != 2:
        stderr.write("cmp: missing operand\n")
        return 2
    left = read_bytes(args[0], stdin)
    right = read_bytes(args[1], stdin)
    if left == right:
        return 0
    if not silent:
        offset = first_difference(left, right)
        stdout.write(f"{args[0]} {args[1]} differ: byte {offset + 1}\n")
    return 1


def utility_diff3(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = [arg for arg in argv[1:] if not arg.startswith("-")]
    if len(args) != 3:
        stderr.write("diff3: expected three files\n")
        return 2
    a = Path(args[0]).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    b = Path(args[1]).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    c = Path(args[2]).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if a == b == c:
        return 0
    stdout.write("====\n")
    stdout.writelines(difflib.unified_diff(a, b, fromfile=args[0], tofile=args[1]))
    stdout.write("====\n")
    stdout.writelines(difflib.unified_diff(b, c, fromfile=args[1], tofile=args[2]))
    return 1


def utility_sdiff(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    args = [arg for arg in argv[1:] if not arg.startswith("-")]
    if len(args) != 2:
        stderr.write("sdiff: missing operand\n")
        return 2
    left = Path(args[0]).read_text(encoding="utf-8", errors="replace").splitlines()
    right = Path(args[1]).read_text(encoding="utf-8", errors="replace").splitlines()
    width = 60
    different = False
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else ""
        b = right[index] if index < len(right) else ""
        marker = " " if a == b else "|"
        different = different or marker != " "
        stdout.write(f"{a:<{width}} {marker} {b}\n")
    return 1 if different else 0


def utility_sed(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    suppress_default = False
    in_place: str | None = None
    scripts: list[str] = []
    paths: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-n":
            suppress_default = True
        elif arg == "-e":
            i += 1
            if i >= len(argv):
                stderr.write("sed: option requires an argument -- e\n")
                return 2
            scripts.append(argv[i])
        elif arg.startswith("-e") and len(arg) > 2:
            scripts.append(arg[2:])
        elif arg == "-f":
            i += 1
            if i >= len(argv):
                stderr.write("sed: option requires an argument -- f\n")
                return 2
            try:
                scripts.append(Path(argv[i]).read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                stderr.write(f"sed: {argv[i]}: {exc.strerror or exc}\n")
                return 2
        elif arg == "-i":
            in_place = ""
        elif arg.startswith("-i"):
            in_place = arg[2:]
        elif arg == "--":
            paths.extend(argv[i + 1 :])
            break
        elif arg.startswith("-"):
            stderr.write(f"sed: unsupported option: {arg}\n")
            return 2
        elif not scripts:
            scripts.append(arg)
        else:
            paths.append(arg)
        i += 1
    if not scripts:
        stderr.write("sed: missing command\n")
        return 2
    try:
        commands = [parse_sed_command(item) for script in scripts for item in split_sed_script(script)]
    except ValueError as exc:
        stderr.write(f"sed: {exc}\n")
        return 2

    if in_place is not None and not paths:
        stderr.write("sed: no input files for in-place editing\n")
        return 2
    status = 0
    input_paths = paths or ["-"]
    for path, text in read_text_inputs(input_paths, stdin, stderr, "sed"):
        if text is None:
            status = 1
            continue
        output = run_sed_program(text, commands, suppress_default)
        if in_place is not None and path != "-":
            file_path = Path(path)
            if in_place:
                backup = file_path.with_name(file_path.name + in_place)
                try:
                    shutil.copy2(file_path, backup)
                except OSError as exc:
                    stderr.write(f"sed: {path}: {exc.strerror or exc}\n")
                    status = 1
                    continue
            try:
                file_path.write_text(output, encoding="utf-8")
            except OSError as exc:
                stderr.write(f"sed: {path}: {exc.strerror or exc}\n")
                status = 1
        else:
            stdout.write(output)
    return status


def utility_gawk(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    fs = " "
    variables: dict[str, str] = {"OFS": " ", "ORS": "\n"}
    program_parts: list[str] = []
    paths: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-F":
            i += 1
            if i >= len(argv):
                stderr.write("gawk: option requires an argument -- F\n")
                return 2
            fs = argv[i]
        elif arg.startswith("-F") and len(arg) > 2:
            fs = arg[2:]
        elif arg == "-v":
            i += 1
            if i >= len(argv) or "=" not in argv[i]:
                stderr.write("gawk: invalid -v assignment\n")
                return 2
            name, value = argv[i].split("=", 1)
            variables[name] = value
        elif arg == "-f":
            i += 1
            if i >= len(argv):
                stderr.write("gawk: option requires an argument -- f\n")
                return 2
            try:
                program_parts.append(Path(argv[i]).read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:
                stderr.write(f"gawk: {argv[i]}: {exc.strerror or exc}\n")
                return 2
        elif arg == "--":
            paths.extend(argv[i + 1 :])
            break
        elif arg.startswith("-"):
            stderr.write(f"gawk: unsupported option: {arg}\n")
            return 2
        elif not program_parts:
            program_parts.append(arg)
        elif "=" in arg and not Path(arg).exists():
            name, value = arg.split("=", 1)
            variables[name] = value
        else:
            paths.append(arg)
        i += 1
    if not program_parts:
        stderr.write("gawk: missing program\n")
        return 2
    variables["FS"] = fs
    try:
        program = parse_awk_program("\n".join(program_parts))
    except ValueError as exc:
        stderr.write(f"gawk: {exc}\n")
        return 2
    status = 0
    state = AwkState(fs=fs, variables=variables)
    try:
        for action in program.begin:
            run_awk_action(action, state, stdout)
        for path, text in read_text_inputs(paths or ["-"], stdin, stderr, "gawk"):
            if text is None:
                status = 1
                continue
            state.filename = path
            state.fnr = 0
            for raw_line in text.splitlines():
                state.nr += 1
                state.fnr += 1
                state.set_record(raw_line)
                for rule in program.rules:
                    if awk_pattern_matches(rule.pattern, state):
                        run_awk_action(rule.action, state, stdout)
        for action in program.end:
            run_awk_action(action, state, stdout)
    except (ValueError, re.error) as exc:
        stderr.write(f"gawk: {exc}\n")
        return 2
    return status


def utility_tar(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    action: str | None = None
    gzip = False
    bzip2 = False
    verbose = False
    archive: str | None = None
    extract_dir = "."
    members: list[tuple[Path, str]] = []
    pending_file = False
    pending_chdir = False
    current_dir = Path(".")

    def parse_option_chars(chars: str) -> int | None:
        nonlocal action, gzip, bzip2, verbose, pending_file, pending_chdir
        for offset, flag in enumerate(chars):
            if flag in {"c", "x", "t"}:
                action = flag
            elif flag == "z":
                gzip = True
            elif flag == "j":
                bzip2 = True
            elif flag == "v":
                verbose = True
            elif flag == "f":
                pending_file = True
                return offset + 1
            elif flag == "C":
                pending_chdir = True
                return offset + 1
            else:
                raise ValueError(f"unsupported option -- {flag}")
        return None

    i = 1
    try:
        while i < len(argv):
            arg = argv[i]
            if pending_file:
                archive = arg
                pending_file = False
            elif pending_chdir:
                current_dir = Path(arg)
                if action == "x":
                    extract_dir = arg
                pending_chdir = False
            elif arg == "--":
                for member in argv[i + 1 :]:
                    members.append((current_dir / member, member))
                break
            elif arg.startswith("-") and arg != "-":
                consumed = parse_option_chars(arg[1:])
                if consumed is not None and consumed < len(arg) - 1:
                    rest = arg[consumed + 1 :]
                    if pending_file:
                        archive = rest
                        pending_file = False
                    elif pending_chdir:
                        current_dir = Path(rest)
                        if action == "x":
                            extract_dir = rest
                        pending_chdir = False
            elif action is None and set(arg) <= set("cxtzjvfC"):
                parse_option_chars(arg)
            else:
                members.append((current_dir / arg, arg))
            i += 1
    except ValueError as exc:
        stderr.write(f"tar: {exc}\n")
        return 2

    if action is None:
        stderr.write("tar: must specify one of -c, -x, or -t\n")
        return 2
    if archive is None:
        stderr.write("tar: archive file required with -f\n")
        return 2
    mode = {"c": "w", "x": "r", "t": "r"}[action]
    if gzip:
        mode += ":gz"
    elif bzip2:
        mode += ":bz2"
    elif action in {"x", "t"}:
        mode += ":*"

    try:
        if action == "c":
            with tarfile.open(archive, mode) as tar:
                for source, arcname in members:
                    tar.add(source, arcname=arcname)
                    if verbose:
                        stdout.write(arcname + "\n")
            return 0
        with tarfile.open(archive, mode) as tar:
            names = [arcname for _source, arcname in members]
            if action == "t":
                for member in tar.getmembers():
                    if names and member.name not in names:
                        continue
                    stdout.write(member.name + "\n")
                return 0
            safe_extract_tar(tar, Path(extract_dir), names)
            if verbose:
                for member in tar.getmembers():
                    if not names or member.name in names:
                        stdout.write(member.name + "\n")
            return 0
    except (OSError, tarfile.TarError) as exc:
        stderr.write(f"tar: {exc}\n")
        return 1

def display_name(entry: Path, base: Path) -> str:
    return entry.name if base.is_dir() else str(entry)


def format_long_listing(path: Path, name: str) -> str:
    try:
        info = path.lstat()
    except OSError:
        return f"?????????? 0 unknown unknown 0 Jan 01 00:00 {name}"
    mode = stat.filemode(info.st_mode)
    mtime = _datetime.datetime.fromtimestamp(info.st_mtime).strftime("%b %d %H:%M")
    owner, group = owner_group_names(info)
    return f"{mode} {getattr(info, 'st_nlink', 1):>2} {owner:<8} {group:<8} {info.st_size:>8} {mtime} {name}"


def parse_line_count_args(command: str, argv: list[str], stderr: TextIO, *, default: int) -> tuple[int, list[str], int]:
    count = default
    paths: list[str] = []
    args = argv[1:]
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-n":
            index += 1
            if index >= len(args):
                stderr.write(f"{command}: option requires an argument -- n\n")
                return count, paths, 2
            try:
                count = int(args[index])
            except ValueError:
                stderr.write(f"{command}: invalid number of lines: {args[index]}\n")
                return count, paths, 2
        elif arg.startswith("-n") and len(arg) > 2:
            try:
                count = int(arg[2:])
            except ValueError:
                stderr.write(f"{command}: invalid number of lines: {arg[2:]}\n")
                return count, paths, 2
        elif arg.startswith("-") and arg[1:].isdigit():
            count = int(arg[1:])
        else:
            paths.append(arg)
        index += 1
    return max(0, count), paths, 0


def write_selected_lines(
    paths: list[str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    selector: Callable[[list[str]], list[str]],
    command: str,
) -> int:
    status = 0
    multiple = len(paths) > 1
    for path, text in read_text_inputs(paths, stdin, stderr, command):
        if text is None:
            status = 1
            continue
        if multiple:
            stdout.write(f"==> {path} <==\n")
        for line in selector(text.splitlines(keepends=True)):
            stdout.write(line)
            if not line.endswith("\n"):
                stdout.write("\n")
    return status


def read_text_inputs(paths: Iterable[str], stdin: TextIO, stderr: TextIO, command: str) -> Iterable[tuple[str, str | None]]:
    for path in paths:
        if path == "-":
            yield path, stdin.read()
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as file:
                yield path, file.read()
        except OSError as exc:
            stderr.write(f"{command}: {path}: {exc.strerror or exc}\n")
            yield path, None


def parse_csv_line(line: str) -> list[str]:
    result: list[str] = []
    field: list[str] = []
    quoted = False
    i = 0
    while i < len(line):
        char = line[i]
        if char == '"':
            if quoted and i + 1 < len(line) and line[i + 1] == '"':
                field.append('"')
                i += 2
                continue
            quoted = not quoted
        elif char == "," and not quoted:
            result.append("".join(field))
            field = []
        else:
            field.append(char)
        i += 1
    result.append("".join(field))
    return result


def read_binary_inputs(paths: Iterable[str], stdin: TextIO) -> Iterable[bytes]:
    for path in paths:
        if path == "-":
            buffer = getattr(stdin, "buffer", None)
            if buffer is not None:
                yield buffer.read()
            else:
                yield stdin.read().encode("utf-8")
            continue
        yield Path(path).read_bytes()


def write_binary_output(stdout: TextIO, data: bytes) -> None:
    buffer = getattr(stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        stdout.write(data.decode("latin-1"))


def expand_tr_set(spec: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(spec):
        first, index = read_tr_unit(spec, index)
        if index < len(spec) - 1 and spec[index] == "-" and len(first) == 1:
            second, next_index = read_tr_unit(spec, index + 1)
            if len(second) == 1:
                start = ord(first)
                end = ord(second)
                step = 1 if start <= end else -1
                result.extend(chr(value) for value in range(start, end + step, step))
                index = next_index
                continue
        result.extend(first)
    return "".join(result)


def read_tr_unit(spec: str, index: int) -> tuple[str, int]:
    if spec.startswith("[:", index):
        end = spec.find(":]", index + 2)
        if end != -1:
            name = spec[index + 2 : end]
            return tr_character_class(name), end + 2
    char = spec[index]
    if char != "\\":
        return char, index + 1
    if index + 1 >= len(spec):
        return "\\", index + 1
    marker = spec[index + 1]
    escapes = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
    }
    if marker in escapes:
        return escapes[marker], index + 2
    if marker in "01234567":
        end = index + 1
        while end < len(spec) and end < index + 4 and spec[end] in "01234567":
            end += 1
        return chr(int(spec[index + 1 : end], 8)), end
    return marker, index + 2


def tr_character_class(name: str) -> str:
    classes = {
        "alnum": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "alpha": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "blank": " \t",
        "digit": "0123456789",
        "lower": "abcdefghijklmnopqrstuvwxyz",
        "space": " \t\r\n\v\f",
        "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "xdigit": "0123456789ABCDEFabcdef",
    }
    return classes.get(name, "")


def squeeze_repeated_chars(text: str, chars: set[str]) -> str:
    if not text or not chars:
        return text
    result: list[str] = []
    previous = ""
    for char in text:
        if char == previous and char in chars:
            continue
        result.append(char)
        previous = char
    return "".join(result)


def owner_group_names(info: os.stat_result) -> tuple[str, str]:
    owner = str(getattr(info, "st_uid", "user"))
    group = str(getattr(info, "st_gid", "group"))
    if pwd is not None:
        try:
            owner = pwd.getpwuid(info.st_uid).pw_name
        except (KeyError, AttributeError):
            pass
    else:
        try:
            owner = getpass.getuser()
        except OSError:
            owner = "user"
    if grp is not None:
        try:
            group = grp.getgrgid(info.st_gid).gr_name
        except (KeyError, AttributeError):
            pass
    elif not group or group.isdigit():
        group = owner
    return owner.replace(" ", "_"), group.replace(" ", "_")


def split_owner_group(spec: str) -> tuple[str | None, str | None]:
    separator = ":" if ":" in spec else "." if "." in spec else ""
    if not separator:
        return spec or None, None
    owner, group = spec.split(separator, 1)
    return owner or None, group or None


def resolve_user_id(owner: str | None) -> int:
    if owner is None:
        return -1
    if owner.isdigit():
        return int(owner)
    if pwd is None:
        raise LookupError(f"unknown user: {owner}")
    return pwd.getpwnam(owner).pw_uid


def resolve_group_id(group: str | None) -> int:
    if group is None:
        return -1
    if group.isdigit():
        return int(group)
    if grp is None:
        raise LookupError(f"unknown group: {group}")
    return grp.getgrnam(group).gr_gid


def apply_owner_group(path: Path, owner: str | None, group: str | None, stderr: TextIO, command: str) -> int:
    if owner is None and group is None:
        return 0
    if not hasattr(os, "chown"):
        stderr.write(f"{command}: changing ownership is not supported on this platform: {path}\n")
        return 1
    try:
        os.chown(path, resolve_user_id(owner), resolve_group_id(group))
    except (LookupError, OSError) as exc:
        stderr.write(f"{command}: {path}: {exc}\n")
        return 1
    return 0


def walk_targets(root: Path, recursive: bool) -> Iterable[Path]:
    yield root
    if not recursive or not root.is_dir() or root.is_symlink():
        return
    for current, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        for directory in dirs:
            yield Path(current) / directory
        for file in files:
            yield Path(current) / file


def unique_adjacent(lines: list[str], key: Callable[[str], object]) -> list[str]:
    result: list[str] = []
    sentinel = object()
    previous: object = sentinel
    for line in lines:
        current = key(line)
        if previous is sentinel or current != previous:
            result.append(line)
        previous = current
    return result


def parse_ranges(spec: str) -> list[tuple[int | None, int | None]]:
    ranges: list[tuple[int | None, int | None]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text) if start_text else None
            end = int(end_text) if end_text else None
        else:
            start = end = int(item)
        if start is not None and start < 1:
            raise ValueError("ranges are numbered from 1")
        if end is not None and end < 1:
            raise ValueError("ranges are numbered from 1")
        ranges.append((start, end))
    return ranges


def select_ranges(text: str, ranges: list[tuple[int | None, int | None]]) -> str:
    selected: list[str] = []
    for start, end in ranges:
        first = 1 if start is None else start
        last = len(text) if end is None else end
        if last < first:
            continue
        selected.extend(text[index - 1] for index in range(first, min(last, len(text)) + 1))
    return "".join(selected)


@dataclass
class FindPredicates:
    names: list[str] = field(default_factory=list)
    inames: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    maxdepth: int | None = None
    mindepth: int = 0
    delete: bool = False
    exec_command: list[str] | None = None
    print_result: bool = True


def parse_find_expression(expr: list[str], stderr: TextIO) -> FindPredicates | None:
    predicates = FindPredicates()
    explicit_action = False
    i = 0
    while i < len(expr):
        token = expr[i]
        if token in {"-a", "-and", "(", ")"}:
            i += 1
            continue
        if token in {"-o", "-or", "!", "-not"}:
            stderr.write(f"find: unsupported expression operator: {token}\n")
            return None
        if token in {"-name", "-iname", "-type", "-maxdepth", "-mindepth"}:
            i += 1
            if i >= len(expr):
                stderr.write(f"find: missing argument to {token}\n")
                return None
            value = expr[i]
            try:
                if token == "-name":
                    predicates.names.append(value)
                elif token == "-iname":
                    predicates.inames.append(value.lower())
                elif token == "-type":
                    if value not in {"f", "d", "l"}:
                        stderr.write(f"find: unknown file type: {value}\n")
                        return None
                    predicates.types.append(value)
                elif token == "-maxdepth":
                    predicates.maxdepth = int(value)
                elif token == "-mindepth":
                    predicates.mindepth = int(value)
            except ValueError:
                stderr.write(f"find: invalid number: {value}\n")
                return None
        elif token == "-print":
            predicates.print_result = True
            explicit_action = True
        elif token == "-delete":
            predicates.delete = True
            predicates.print_result = False
            explicit_action = True
        elif token == "-exec":
            command: list[str] = []
            i += 1
            while i < len(expr) and expr[i] not in {";", "+"}:
                command.append(expr[i])
                i += 1
            if i >= len(expr) or not command:
                stderr.write("find: missing argument to -exec\n")
                return None
            predicates.exec_command = command
            predicates.print_result = False
            explicit_action = True
        else:
            stderr.write(f"find: unknown predicate: {token}\n")
            return None
        i += 1
    if not explicit_action:
        predicates.print_result = True
    return predicates


def iter_find_paths(root: Path, maxdepth: int | None) -> Iterable[Path]:
    yield root
    if maxdepth == 0 or not root.is_dir() or root.is_symlink():
        return
    for current, dirs, files in os.walk(root):
        current_path = Path(current)
        try:
            depth = len(current_path.resolve().relative_to(root.resolve()).parts)
        except ValueError:
            depth = max(0, len(current_path.parts) - len(root.parts))
        if maxdepth is not None and depth >= maxdepth:
            dirs[:] = []
            continue
        dirs.sort()
        files.sort()
        for directory in dirs:
            yield current_path / directory
        for file in files:
            yield current_path / file


def find_matches(path: Path, predicates: FindPredicates) -> bool:
    if predicates.names and not any(fnmatch.fnmatchcase(path.name, pattern) for pattern in predicates.names):
        return False
    if predicates.inames and not any(fnmatch.fnmatchcase(path.name.lower(), pattern) for pattern in predicates.inames):
        return False
    if predicates.types:
        path_type = "l" if path.is_symlink() else "d" if path.is_dir() else "f" if path.is_file() else ""
        if path_type not in predicates.types:
            return False
    return True


def locate_db_path() -> Path:
    return Path(os.environ.get("PY_POSIX_SHELL_LOCATE_DB", str(Path.home() / ".py_posix_shell_locatedb")))


def shlex_split(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    size = max(1, size)
    for index in range(0, len(items), size):
        yield items[index : index + size]


def read_file_or_stdin(path: str, stdin: TextIO) -> list[str]:
    if path == "-":
        return stdin.read().splitlines(keepends=True)
    return Path(path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def read_bytes(path: str, stdin: TextIO) -> bytes:
    if path == "-":
        return stdin.read().encode("utf-8")
    return Path(path).read_bytes()


def first_difference(left: bytes, right: bytes) -> int:
    for index, (left_byte, right_byte) in enumerate(zip(left, right)):
        if left_byte != right_byte:
            return index
    return min(len(left), len(right))


@dataclass
class SedCommand:
    address: tuple[str, Any] | None
    command: str
    pattern: str = ""
    replacement: str = ""
    flags: str = ""


def split_sed_script(script: str) -> list[str]:
    commands: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in script:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if current and "".join(current).lstrip().endswith("s") and char not in {" ", "\t"}:
            quote = char
            current.append(char)
            continue
        if char in {";", "\n"}:
            item = "".join(current).strip()
            if item:
                commands.append(item)
            current = []
        else:
            current.append(char)
    item = "".join(current).strip()
    if item:
        commands.append(item)
    return commands


def read_until_unescaped(text: str, start: int, delimiter: str) -> tuple[str, int]:
    result: list[str] = []
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if escaped:
            result.append("\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == delimiter:
            return "".join(result), index + 1
        else:
            result.append(char)
        index += 1
    raise ValueError("unterminated command")


def parse_sed_command(command: str) -> SedCommand:
    text = command.strip()
    index = 0
    address: tuple[str, Any] | None = None
    if index < len(text) and text[index].isdigit():
        start = index
        while index < len(text) and text[index].isdigit():
            index += 1
        address = ("line", int(text[start:index]))
    elif index < len(text) and text[index] == "$":
        address = ("last", None)
        index += 1
    elif index < len(text) and text[index] == "/":
        pattern, index = read_until_unescaped(text, index + 1, "/")
        address = ("regex", re.compile(pattern))
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        raise ValueError("missing command")
    op = text[index]
    index += 1
    if op == "s":
        if index >= len(text):
            raise ValueError("unterminated substitute")
        delimiter = text[index]
        pattern, index = read_until_unescaped(text, index + 1, delimiter)
        replacement, index = read_until_unescaped(text, index, delimiter)
        flags = text[index:].strip()
        return SedCommand(address, "s", pattern, normalize_sed_replacement(replacement), flags)
    if op in {"p", "d", "q"}:
        return SedCommand(address, op)
    raise ValueError(f"unsupported command: {op}")


def normalize_sed_replacement(replacement: str) -> str:
    result: list[str] = []
    escaped = False
    for char in replacement:
        if escaped:
            result.append("\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "&":
            result.append(r"\g<0>")
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def sed_address_matches(address: tuple[str, Any] | None, line: str, number: int, total: int) -> bool:
    if address is None:
        return True
    kind, value = address
    if kind == "line":
        return number == value
    if kind == "last":
        return number == total
    if kind == "regex":
        return value.search(line) is not None
    return False


def run_sed_program(text: str, commands: list[SedCommand], suppress_default: bool) -> str:
    output: list[str] = []
    lines = text.splitlines()
    total = len(lines)
    for number, original in enumerate(lines, 1):
        line = original
        deleted = False
        for command in commands:
            if not sed_address_matches(command.address, line, number, total):
                continue
            if command.command == "s":
                flags = re.IGNORECASE if "i" in command.flags or "I" in command.flags else 0
                count = 0 if "g" in command.flags else 1
                new_line, changed = re.subn(command.pattern, command.replacement, line, count=count, flags=flags)
                line = new_line
                if changed and "p" in command.flags:
                    output.append(line + "\n")
            elif command.command == "p":
                output.append(line + "\n")
            elif command.command == "d":
                deleted = True
                break
            elif command.command == "q":
                if not suppress_default:
                    output.append(line + "\n")
                return "".join(output)
        if not deleted and not suppress_default:
            output.append(line + "\n")
    return "".join(output)


@dataclass
class AwkRule:
    pattern: str
    action: str


@dataclass
class AwkProgram:
    begin: list[str] = field(default_factory=list)
    rules: list[AwkRule] = field(default_factory=list)
    end: list[str] = field(default_factory=list)


@dataclass
class AwkState:
    fs: str
    variables: dict[str, str]
    record: str = ""
    fields: list[str] = field(default_factory=list)
    nr: int = 0
    fnr: int = 0
    filename: str = "-"

    def set_record(self, line: str) -> None:
        self.record = line
        if self.fs == " ":
            self.fields = line.split()
        else:
            self.fields = re.split(self.fs, line)
        self.variables["NR"] = str(self.nr)
        self.variables["FNR"] = str(self.fnr)
        self.variables["NF"] = str(len(self.fields))
        self.variables["FILENAME"] = self.filename


def parse_awk_program(source: str) -> AwkProgram:
    program = AwkProgram()
    position = 0
    found = False
    while True:
        open_index = source.find("{", position)
        if open_index == -1:
            trailing = source[position:].strip()
            if trailing and not found:
                program.rules.append(AwkRule(trailing, "print"))
            elif trailing:
                raise ValueError(f"unexpected text: {trailing}")
            break
        pattern = source[position:open_index].strip()
        close_index = find_matching_brace(source, open_index)
        action = source[open_index + 1 : close_index].strip()
        keyword = pattern.upper()
        if keyword == "BEGIN":
            program.begin.append(action)
        elif keyword == "END":
            program.end.append(action)
        else:
            program.rules.append(AwkRule(pattern, action or "print"))
        found = True
        position = close_index + 1
    if not program.rules and not program.begin and not program.end:
        raise ValueError("empty program")
    return program


def find_matching_brace(source: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("missing closing brace")


def split_awk_statements(action: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    parens = 0
    for char in action:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
        elif char == "(":
            current.append(char)
            parens += 1
        elif char == ")":
            current.append(char)
            parens = max(0, parens - 1)
        elif char in {";", "\n"} and parens == 0:
            item = "".join(current).strip()
            if item:
                statements.append(item)
            current = []
        else:
            current.append(char)
    item = "".join(current).strip()
    if item:
        statements.append(item)
    return statements


def split_awk_arguments(text: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    parens = 0
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
        elif char == "(":
            current.append(char)
            parens += 1
        elif char == ")":
            current.append(char)
            parens = max(0, parens - 1)
        elif char == "," and parens == 0:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    item = "".join(current).strip()
    if item:
        args.append(item)
    return args


def awk_pattern_matches(pattern: str, state: AwkState) -> bool:
    pattern = pattern.strip()
    if not pattern:
        return True
    if pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], state.record) is not None
    if pattern.startswith("!") and pattern[1:].strip().startswith("/") and pattern.endswith("/"):
        inner = pattern[1:].strip()
        return re.search(inner[1:-1], state.record) is None
    match = re.match(r"(.+?)\s*(==|!=|>=|<=|>|<|~|!~)\s*(.+)", pattern)
    if match:
        left = eval_awk_expr(match.group(1), state)
        op = match.group(2)
        right = eval_awk_expr(match.group(3), state)
        if op == "~":
            return re.search(str(right), str(left)) is not None
        if op == "!~":
            return re.search(str(right), str(left)) is None
        return compare_awk_values(left, right, op)
    value = eval_awk_expr(pattern, state)
    return bool(value) and value != "0"


def compare_awk_values(left: object, right: object, op: str) -> bool:
    left_num = to_number(left)
    right_num = to_number(right)
    if left_num is not None and right_num is not None:
        a: object = left_num
        b: object = right_num
    else:
        a = str(left)
        b = str(right)
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b  # type: ignore[operator]
    if op == "<":
        return a < b  # type: ignore[operator]
    if op == ">=":
        return a >= b  # type: ignore[operator]
    if op == "<=":
        return a <= b  # type: ignore[operator]
    return False


def to_number(value: object) -> float | None:
    try:
        return float(str(value))
    except ValueError:
        return None


def eval_awk_expr(expression: str, state: AwkState) -> object:
    expression = expression.strip()
    if expression == "":
        return ""
    if expression.startswith('"') or expression.startswith("'"):
        try:
            return ast.literal_eval(expression)
        except (SyntaxError, ValueError):
            return expression.strip("\"'")
    if expression == "$0":
        return state.record
    if expression.startswith("$") and expression[1:].isdigit():
        index = int(expression[1:])
        return state.fields[index - 1] if 1 <= index <= len(state.fields) else ""
    if expression in {"NR", "FNR", "NF", "FILENAME"}:
        return state.variables.get(expression, "")
    if expression in state.variables:
        return state.variables[expression]
    call = re.match(r"(length|tolower|toupper)\((.*)\)$", expression)
    if call:
        value = str(eval_awk_expr(call.group(2), state))
        if call.group(1) == "length":
            return len(value)
        if call.group(1) == "tolower":
            return value.lower()
        return value.upper()
    if re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", expression):
        return float(expression) if "." in expression else int(expression)
    if "+" in expression:
        parts = [eval_awk_expr(part, state) for part in expression.split("+")]
        if all(to_number(part) is not None for part in parts):
            return sum(float(str(part)) for part in parts)
        return "".join(str(part) for part in parts)
    tokens = split_awk_concat(expression)
    if len(tokens) > 1:
        return "".join(str(eval_awk_expr(token, state)) for token in tokens)
    return expression


def split_awk_concat(expression: str) -> list[str]:
    lexer = shlex.shlex(expression, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return [expression]


def run_awk_action(action: str, state: AwkState, stdout: TextIO) -> None:
    for statement in split_awk_statements(action):
        if statement == "next":
            break
        if statement.startswith("print"):
            rest = statement[5:].strip()
            values = [state.record] if not rest else [str(eval_awk_expr(arg, state)) for arg in split_awk_arguments(rest)]
            stdout.write(str(state.variables.get("OFS", " ")).join(values) + str(state.variables.get("ORS", "\n")))
        elif statement.startswith("printf"):
            rest = statement[6:].strip()
            args = split_awk_arguments(rest)
            if not args:
                continue
            fmt = str(eval_awk_expr(args[0], state))
            values = tuple(eval_awk_expr(arg, state) for arg in args[1:])
            stdout.write(fmt % values if values else fmt)
        elif "=" in statement:
            name, expr = statement.split("=", 1)
            state.variables[name.strip()] = str(eval_awk_expr(expr, state))
        else:
            eval_awk_expr(statement, state)


def safe_extract_tar(tar: tarfile.TarFile, destination: Path, members: list[str]) -> None:
    destination = destination.resolve()
    selected = [member for member in tar.getmembers() if not members or member.name in members]
    for member in selected:
        target = (destination / member.name).resolve()
        if destination != target and destination not in target.parents:
            raise tarfile.TarError(f"refusing to extract outside destination: {member.name}")
    try:
        tar.extractall(destination, members=selected, filter="data")
    except TypeError:  # Python 3.10/3.11 compatibility.
        tar.extractall(destination, members=selected)


def sys_argv0() -> str:
    try:
        import sys

        return sys.argv[0]
    except Exception:
        return "python"


INTERNAL_UTILITIES: dict[str, Utility] = {
    "awk": utility_gawk,
    "basename": utility_basename,
    "base64": utility_base64,
    "cat": utility_cat,
    "chmod": utility_chmod,
    "chgrp": utility_chgrp,
    "chown": utility_chown,
    "clear": utility_clear,
    "cmp": utility_cmp,
    "cp": utility_cp,
    "cut": utility_cut,
    "date": utility_date,
    "diff": utility_diff,
    "diff3": utility_diff3,
    "dirname": utility_dirname,
    "echo": utility_echo,
    "egrep": utility_grep,
    "fgrep": utility_grep,
    "find": utility_find,
    "gawk": utility_gawk,
    "grep": utility_grep,
    "head": utility_head,
    "id": utility_id,
    "install": utility_install,
    "kill": utility_kill,
    "ln": utility_ln,
    "locate": utility_locate,
    "ls": utility_ls,
    "mkdir": utility_mkdir,
    "mv": utility_mv,
    "ps": utility_ps,
    "pwd": utility_pwd,
    "printf": utility_printf,
    "rm": utility_rm,
    "rmdir": utility_rmdir,
    "sdiff": utility_sdiff,
    "sed": utility_sed,
    "sleep": utility_sleep,
    "sort": utility_sort,
    "tail": utility_tail,
    "tar": utility_tar,
    "touch": utility_touch,
    "tr": utility_tr,
    "uname": utility_uname,
    "uniq": utility_uniq,
    "unlink": utility_unlink,
    "updatedb": utility_updatedb,
    "wc": utility_wc,
    "whoami": utility_whoami,
    "xargs": utility_xargs,
    "yes": utility_yes,
}
