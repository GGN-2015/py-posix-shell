"""Small POSIX utility fallbacks used when host commands are unavailable."""

from __future__ import annotations

import ast
import base64
import binascii
import datetime as _datetime
import difflib
import fnmatch
import getpass
import json
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


@dataclass(frozen=True)
class StorageVolume:
    device: str
    mountpoint: str
    fstype: str
    label: str
    total: int
    used: int
    free: int
    drive_type: str = "disk"
    removable: bool = False
    readonly: bool = False
    serial: str = ""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    user: str
    tty: str
    cpu_seconds: float
    start_time: _datetime.datetime | None
    cmd: str
    name: str
    rss: int = 0
    virtual_size: int = 0
    state: str = "S"


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


def utility_df(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    del shell, stdin
    human = False
    show_type = False
    total_row = False
    block_size = 1024
    block_header = "1K-blocks"
    paths: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            paths.extend(argv[index + 1 :])
            break
        if arg == "--help":
            stdout.write("Usage: df [-hkmPT] [file ...]\n")
            return 0
        if arg == "--human-readable":
            human = True
        elif arg == "--total":
            total_row = True
        elif arg.startswith("--block-size="):
            parsed = parse_size_argument(arg.split("=", 1)[1])
            if parsed is None:
                stderr.write(f"df: invalid block size: {arg.split('=', 1)[1]}\n")
                return 2
            block_size = parsed
            block_header = f"{block_size}-blocks"
        elif arg == "-B" or arg == "--block-size":
            index += 1
            if index >= len(argv):
                stderr.write("df: option requires an argument -- B\n")
                return 2
            parsed = parse_size_argument(argv[index])
            if parsed is None:
                stderr.write(f"df: invalid block size: {argv[index]}\n")
                return 2
            block_size = parsed
            block_header = f"{block_size}-blocks"
        elif arg.startswith("-B") and len(arg) > 2:
            parsed = parse_size_argument(arg[2:])
            if parsed is None:
                stderr.write(f"df: invalid block size: {arg[2:]}\n")
                return 2
            block_size = parsed
            block_header = f"{block_size}-blocks"
        elif arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "h":
                    human = True
                elif flag == "k":
                    block_size = 1024
                    block_header = "1K-blocks"
                elif flag == "m":
                    block_size = 1024 * 1024
                    block_header = "1M-blocks"
                elif flag in {"P", "a"}:
                    pass
                elif flag == "T":
                    show_type = True
                else:
                    stderr.write(f"df: invalid option -- {flag}\n")
                    return 2
        else:
            paths.append(arg)
        index += 1

    try:
        volumes = collect_storage_volumes(paths or None)
    except OSError as exc:
        stderr.write(f"df: {exc.filename or ''}: {exc.strerror or exc}\n")
        return 1

    headers = ["Filesystem"]
    if show_type:
        headers.append("Type")
    headers.extend(["Size", "Used", "Avail", "Use%", "Mounted on"] if human else [block_header, "Used", "Available", "Use%", "Mounted on"])
    rows: list[list[str]] = []
    total = used = free = 0
    for volume in volumes:
        total += volume.total
        used += volume.used
        free += volume.free
        rows.append(format_df_row(volume, human=human, show_type=show_type, block_size=block_size))
    if total_row and volumes:
        rows.append(
            format_df_row(
                StorageVolume("total", "total", "", "", total, used, free),
                human=human,
                show_type=show_type,
                block_size=block_size,
            )
        )
    write_aligned_table(stdout, headers, rows, right_align={"Size", "Used", "Avail", "Available", block_header, "Use%"})
    return 0


def utility_lsblk(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    del shell, stdin
    fs_mode = False
    bytes_mode = False
    no_headings = False
    no_deps = False
    ascii_tree = False
    columns: list[str] | None = None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break
        if arg == "--help":
            stdout.write("Usage: lsblk [-bdfin] [-o column[,column...]]\n")
            return 0
        if arg in {"-o", "--output"}:
            index += 1
            if index >= len(argv):
                stderr.write("lsblk: option requires an argument -- o\n")
                return 2
            columns = parse_lsblk_columns(argv[index])
        elif arg.startswith("--output="):
            columns = parse_lsblk_columns(arg.split("=", 1)[1])
        elif arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "f":
                    fs_mode = True
                elif flag == "b":
                    bytes_mode = True
                elif flag == "d":
                    no_deps = True
                elif flag == "n":
                    no_headings = True
                elif flag == "i":
                    ascii_tree = True
                elif flag == "o":
                    stderr.write("lsblk: option requires an argument -- o\n")
                    return 2
                else:
                    stderr.write(f"lsblk: invalid option -- {flag}\n")
                    return 2
        else:
            stderr.write(f"lsblk: unexpected operand: {arg}\n")
            return 2
        index += 1

    if columns is None:
        columns = ["NAME", "FSTYPE", "LABEL", "UUID", "FSAVAIL", "FSUSE%", "MOUNTPOINTS"] if fs_mode else [
            "NAME",
            "MAJ:MIN",
            "RM",
            "SIZE",
            "RO",
            "TYPE",
            "MOUNTPOINTS",
        ]
    unknown = [column for column in columns if column not in LSBLK_COLUMN_NAMES]
    if unknown:
        stderr.write(f"lsblk: unknown column: {unknown[0]}\n")
        return 2

    try:
        volumes = collect_storage_volumes(None)
    except OSError as exc:
        stderr.write(f"lsblk: {exc.strerror or exc}\n")
        return 1

    rows = build_lsblk_rows(volumes, columns, bytes_mode=bytes_mode, no_deps=no_deps, ascii_tree=ascii_tree)
    write_aligned_table(stdout, [] if no_headings else columns, rows, right_align={"MAJ:MIN", "RM", "SIZE", "RO", "FSAVAIL", "FSUSE%"})
    return 0


def utility_help(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    from . import __version__

    topics = argv[1:]
    if not topics:
        stdout.write(f"py-posix-shell, version {__version__}\n")
        stdout.write("These shell commands and fallback utilities are defined internally.\n")
        stdout.write("Type `help NAME' for a short description of NAME.\n\n")
        write_help_table(stdout, HELP_SUMMARY_ITEMS)
        return 0

    status = 0
    for index, topic in enumerate(topics):
        key = topic.strip()
        text = HELP_DETAILS.get(key)
        if text is None:
            stderr.write(f"help: no help topics match '{topic}'\n")
            status = 1
            continue
        if index:
            stdout.write("\n")
        stdout.write(text.rstrip() + "\n")
    return status


def utility_vi(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    if os.name != "nt":
        stderr.write("vi: fallback editor is only available on Windows when vi/vim is missing\n")
        return 127

    paths = [arg for arg in argv[1:] if arg != "--" and not arg.startswith("-")]
    file_path = Path(paths[0]) if paths else None
    if not stream_is_tty(stdin) or not stream_is_tty(stdout):
        stderr.write("vi: fallback editor requires a TTY; install vi or vim for non-interactive use\n")
        return 2

    editor = WindowsViEditor(file_path, stdout, stderr)
    try:
        return editor.run()
    except KeyboardInterrupt:
        return 130


class WindowsViEditor:
    def __init__(self, file_path: Path | None, stdout: TextIO, stderr: TextIO) -> None:
        self.file_path = file_path
        self.stdout = stdout
        self.stderr = stderr
        self.lines = [""]
        self.cursor_line = 0
        self.cursor_col = 0
        self.top_line = 0
        self.left_col = 0
        self.mode = "NORMAL"
        self.command = ""
        self.status = "py-posix-shell vi fallback"
        self.dirty = False
        self.exit_status: int | None = None
        self.pending = ""
        self.load()

    def load(self) -> None:
        if self.file_path is None or not self.file_path.exists():
            self.lines = [""]
            return
        try:
            text = self.file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.status = f"{self.file_path}: {exc.strerror or exc}"
            self.lines = [""]
            return
        self.lines = text.splitlines() or [""]
        self.status = f'"{self.file_path}" {len(self.lines)} lines'

    def run(self) -> int:
        import msvcrt

        self.enter_screen()
        try:
            while self.exit_status is None:
                self.render()
                key = self.read_key(msvcrt)
                self.handle_key(key)
        finally:
            self.leave_screen()
        return self.exit_status

    def enter_screen(self) -> None:
        self.enable_virtual_terminal()
        self.stdout.write("\033[?1049h\033[?25h\033[2J\033[H")
        self.stdout.flush()

    def leave_screen(self) -> None:
        self.stdout.write("\033[?25h\033[?1049l")
        self.stdout.flush()

    def enable_virtual_terminal(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
        except ImportError:
            return

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        if handle in {-1, 0}:
            return
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)

    def read_key(self, msvcrt_module) -> str:
        char = msvcrt_module.getwch()
        if char in {"\x00", "\xe0"}:
            code = msvcrt_module.getwch()
            return {
                "H": "UP",
                "P": "DOWN",
                "K": "LEFT",
                "M": "RIGHT",
                "G": "HOME",
                "O": "END",
                "S": "DELETE",
            }.get(code, "")
        if char == "\x03":
            raise KeyboardInterrupt
        if char == "\x1b":
            return "ESC"
        if char in {"\r", "\n"}:
            return "ENTER"
        if char in {"\b", "\x7f"}:
            return "BACKSPACE"
        return char

    def render(self) -> None:
        columns, rows = shutil.get_terminal_size((80, 24))
        text_rows = max(1, rows - 2)
        self.keep_cursor_visible(text_rows, columns)
        self.stdout.write("\033[?25l\033[H")
        for screen_row in range(text_rows):
            line_index = self.top_line + screen_row
            if line_index < len(self.lines):
                text = self.display_line(self.lines[line_index])[self.left_col : self.left_col + columns]
            else:
                text = "~"
            self.stdout.write("\033[K" + text + "\n")
        name = str(self.file_path) if self.file_path is not None else "[No Name]"
        modified = " [+]" if self.dirty else ""
        status = f" {self.mode}  {name}{modified}  Ln {self.cursor_line + 1}, Col {self.cursor_col + 1} "
        self.stdout.write("\033[7m" + status[:columns].ljust(columns) + "\033[0m\n")
        command_line = ":" + self.command if self.mode == "COMMAND" else self.status
        self.stdout.write("\033[K" + command_line[:columns])
        screen_y = self.cursor_line - self.top_line + 1
        visual_col = self.visual_cursor_column(self.current_line(), self.cursor_col)
        screen_x = max(1, min(visual_col - self.left_col + 1, columns))
        if self.mode == "COMMAND":
            screen_y = rows
            screen_x = min(len(self.command) + 2, columns)
        self.stdout.write(f"\033[{screen_y};{screen_x}H\033[?25h")
        self.stdout.flush()

    def display_line(self, line: str) -> str:
        return "".join(char if char >= " " else " " for char in line.expandtabs(4))

    def visual_cursor_column(self, line: str, column: int) -> int:
        visual_col = 0
        for char in line[:column]:
            if char == "\t":
                visual_col += 4 - (visual_col % 4)
            else:
                visual_col += 1
        return visual_col

    def keep_cursor_visible(self, text_rows: int, columns: int) -> None:
        if self.cursor_line < self.top_line:
            self.top_line = self.cursor_line
        elif self.cursor_line >= self.top_line + text_rows:
            self.top_line = self.cursor_line - text_rows + 1
        visual_col = self.visual_cursor_column(self.current_line(), self.cursor_col)
        if visual_col < self.left_col:
            self.left_col = visual_col
        elif visual_col >= self.left_col + columns:
            self.left_col = visual_col - columns + 1
        if self.visual_cursor_column(self.current_line(), len(self.current_line())) < columns:
            self.left_col = 0

    def handle_key(self, key: str) -> None:
        if self.mode == "INSERT":
            self.handle_insert_key(key)
        elif self.mode == "COMMAND":
            self.handle_command_key(key)
        else:
            self.handle_normal_key(key)

    def handle_normal_key(self, key: str) -> None:
        if key in {"h", "LEFT"}:
            self.move_left()
        elif key in {"j", "DOWN"}:
            self.move_down()
        elif key in {"k", "UP"}:
            self.move_up()
        elif key in {"l", "RIGHT"}:
            self.move_right()
        elif key == "HOME" or key == "0":
            self.cursor_col = 0
        elif key == "END" or key == "$":
            self.cursor_col = len(self.current_line())
        elif key == "i":
            self.mode = "INSERT"
            self.status = "-- INSERT --"
        elif key == "a":
            self.move_right(allow_end=True)
            self.mode = "INSERT"
            self.status = "-- INSERT --"
        elif key == "o":
            self.cursor_line += 1
            self.lines.insert(self.cursor_line, "")
            self.cursor_col = 0
            self.mode = "INSERT"
            self.dirty = True
        elif key == "O":
            self.lines.insert(self.cursor_line, "")
            self.cursor_col = 0
            self.mode = "INSERT"
            self.dirty = True
        elif key == "x" or key == "DELETE":
            self.delete_char()
        elif key == "d":
            if self.pending == "d":
                self.delete_line()
                self.pending = ""
            else:
                self.pending = "d"
                return
        elif key == ":":
            self.mode = "COMMAND"
            self.command = ""
        elif key == "ESC":
            self.pending = ""
        self.pending = "" if key != "d" else self.pending

    def handle_insert_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = "NORMAL"
            self.status = ""
            if self.cursor_col > 0:
                self.cursor_col -= 1
        elif key == "ENTER":
            line = self.current_line()
            before, after = line[: self.cursor_col], line[self.cursor_col :]
            self.lines[self.cursor_line] = before
            self.lines.insert(self.cursor_line + 1, after)
            self.cursor_line += 1
            self.cursor_col = 0
            self.dirty = True
        elif key == "BACKSPACE":
            self.backspace()
        elif key == "LEFT":
            self.move_left()
        elif key == "RIGHT":
            self.move_right(allow_end=True)
        elif key == "UP":
            self.move_up()
        elif key == "DOWN":
            self.move_down()
        elif len(key) == 1 and key >= " ":
            line = self.current_line()
            self.lines[self.cursor_line] = line[: self.cursor_col] + key + line[self.cursor_col :]
            self.cursor_col += 1
            self.dirty = True

    def handle_command_key(self, key: str) -> None:
        if key == "ESC":
            self.mode = "NORMAL"
            self.command = ""
        elif key == "BACKSPACE":
            self.command = self.command[:-1]
        elif key == "ENTER":
            self.execute_command(self.command.strip())
            self.command = ""
            if self.exit_status is None:
                self.mode = "NORMAL"
        elif len(key) == 1 and key >= " ":
            self.command += key

    def execute_command(self, command: str) -> None:
        if command in {"q", "quit"}:
            if self.dirty:
                self.status = "No write since last change (add ! to override)"
            else:
                self.exit_status = 0
        elif command in {"q!", "quit!"}:
            self.exit_status = 0
        elif command in {"w", "write"} or command.startswith("w "):
            target = command.split(maxsplit=1)[1] if " " in command else ""
            self.write_file(Path(target) if target else self.file_path)
        elif command in {"wq", "x"}:
            if self.write_file(self.file_path):
                self.exit_status = 0
        elif command.startswith("wq "):
            if self.write_file(Path(command.split(maxsplit=1)[1])):
                self.exit_status = 0
        else:
            self.status = f"Not an editor command: {command}"

    def write_file(self, path: Path | None) -> bool:
        if path is None:
            self.status = "No file name"
            return False
        try:
            path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        except OSError as exc:
            self.status = f"{path}: {exc.strerror or exc}"
            return False
        self.file_path = path
        self.dirty = False
        self.status = f'"{path}" {len(self.lines)} lines written'
        return True

    def current_line(self) -> str:
        return self.lines[self.cursor_line]

    def move_left(self) -> None:
        if self.cursor_col > 0:
            self.cursor_col -= 1

    def move_right(self, *, allow_end: bool = False) -> None:
        limit = len(self.current_line()) if allow_end else max(0, len(self.current_line()) - 1)
        if self.cursor_col < limit:
            self.cursor_col += 1

    def move_up(self) -> None:
        if self.cursor_line > 0:
            self.cursor_line -= 1
            self.cursor_col = min(self.cursor_col, len(self.current_line()))

    def move_down(self) -> None:
        if self.cursor_line + 1 < len(self.lines):
            self.cursor_line += 1
            self.cursor_col = min(self.cursor_col, len(self.current_line()))

    def delete_char(self) -> None:
        line = self.current_line()
        if self.cursor_col < len(line):
            self.lines[self.cursor_line] = line[: self.cursor_col] + line[self.cursor_col + 1 :]
            self.dirty = True

    def delete_line(self) -> None:
        if len(self.lines) == 1:
            self.lines[0] = ""
            self.cursor_col = 0
        else:
            del self.lines[self.cursor_line]
            if self.cursor_line >= len(self.lines):
                self.cursor_line = len(self.lines) - 1
            self.cursor_col = min(self.cursor_col, len(self.current_line()))
        self.dirty = True

    def backspace(self) -> None:
        if self.cursor_col > 0:
            line = self.current_line()
            self.lines[self.cursor_line] = line[: self.cursor_col - 1] + line[self.cursor_col :]
            self.cursor_col -= 1
            self.dirty = True
        elif self.cursor_line > 0:
            previous_len = len(self.lines[self.cursor_line - 1])
            self.lines[self.cursor_line - 1] += self.lines[self.cursor_line]
            del self.lines[self.cursor_line]
            self.cursor_line -= 1
            self.cursor_col = previous_len
            self.dirty = True


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
    if os.name != "nt":
        stderr.write("ps: fallback implementation is only available on Windows\n")
        return 127

    mode = parse_ps_mode(argv, stderr)
    if mode is None:
        return 2
    if mode == "help":
        stdout.write("Usage: ps [aux|-ef]\n")
        return 0
    detailed = mode in {"ef", "aux"}
    processes = collect_windows_processes(detailed=detailed)
    if not processes:
        processes = [current_process_info()]
    tty = "con" if stream_is_tty(stdin) or stream_is_tty(stdout) else "?"
    if mode == "default":
        selected = filter_current_process_tree(processes, os.getpid())
        if not selected:
            selected = [current_process_info()]
        write_ps_default(stdout, selected, tty)
    elif mode == "ef":
        write_ps_ef(stdout, processes, tty)
    else:
        write_ps_aux(stdout, processes, tty)
    return 0


def utility_which(shell, argv: list[str], stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    del stdin
    if os.name != "nt":
        stderr.write("which: fallback implementation is only available on Windows\n")
        return 127

    show_all = False
    names: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            names.extend(argv[index + 1 :])
            break
        if arg in {"-a", "--all"}:
            show_all = True
        elif arg == "--help":
            stdout.write("Usage: which [-a] name ...\n")
            return 0
        elif arg.startswith("-") and arg != "-":
            for flag in arg[1:]:
                if flag == "a":
                    show_all = True
                else:
                    stderr.write(f"which: invalid option -- {flag}\n")
                    return 2
        else:
            names.append(arg)
        index += 1

    if not names:
        stderr.write("which: missing operand\n")
        return 2

    status = 0
    for name in names:
        matches = which_command_matches(shell, name)
        if not matches:
            status = 1
            continue
        for kind, value in (matches if show_all else matches[:1]):
            if kind == "builtin":
                stdout.write(f"{value}: shell built-in command\n")
            elif kind == "utility":
                stdout.write(f"{value}: shell utility\n")
            else:
                stdout.write(value + "\n")
    return status


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


def stream_is_tty(stream: TextIO) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError:
        return False


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


def parse_ps_mode(argv: list[str], stderr: TextIO) -> str | None:
    if len(argv) == 1:
        return "default"
    saw_all = False
    saw_full = False
    bsd_flags = ""
    for arg in argv[1:]:
        if arg == "--":
            break
        if arg in {"--help"}:
            return "help"
        if arg in {"aux", "axu", "uxa"}:
            return "aux"
        if arg.startswith("-") and arg != "-":
            flags = arg[1:]
            if set(flags) <= {"a", "u", "x"} and {"a", "u", "x"}.issubset(set(flags)):
                return "aux"
            for flag in flags:
                if flag in {"e", "A"}:
                    saw_all = True
                elif flag == "f":
                    saw_full = True
                else:
                    stderr.write(f"ps: unsupported option -- {flag}\n")
                    return None
        elif set(arg) <= {"a", "u", "x"}:
            bsd_flags += arg
            if {"a", "u", "x"}.issubset(set(bsd_flags)):
                return "aux"
        else:
            stderr.write(f"ps: unsupported operand: {arg}\n")
            return None
    if saw_all and saw_full:
        return "ef"
    if saw_all:
        return "ef"
    if saw_full:
        return "ef"
    return "default"


def collect_windows_processes(*, detailed: bool) -> list[ProcessInfo]:
    if os.name != "nt":
        return []
    if detailed:
        processes = collect_windows_processes_powershell()
        if processes:
            return processes
    processes = collect_windows_processes_toolhelp()
    return processes or [current_process_info()]


def collect_windows_processes_powershell() -> list[ProcessInfo]:
    powershell = find_powershell_executable()
    if not powershell:
        return []
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Get-CimInstance Win32_Process | ForEach-Object {
  $owner = ''
  try {
    $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop
    if ($o.User) {
      if ($o.Domain) { $owner = "$($o.Domain)\$($o.User)" } else { $owner = $o.User }
    }
  } catch {}
  [PSCustomObject]@{
    PID = [int]$_.ProcessId
    PPID = [int]$_.ParentProcessId
    Name = [string]$_.Name
    CommandLine = [string]$_.CommandLine
    Owner = [string]$owner
    CreationDate = if ($_.CreationDate) { $_.CreationDate.ToString('o') } else { '' }
    KernelModeTime = [string]$_.KernelModeTime
    UserModeTime = [string]$_.UserModeTime
    WorkingSetSize = [string]$_.WorkingSetSize
    VirtualSize = [string]$_.VirtualSize
  }
} | ConvertTo-Json -Compress -Depth 3
"""
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    return parse_windows_process_json(completed.stdout)


def find_powershell_executable() -> str | None:
    candidates = [
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        shutil.which("powershell.exe"),
        shutil.which("pwsh.exe"),
    ]
    for candidate in dict.fromkeys(item for item in candidates if item):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def parse_windows_process_json(text: str) -> list[ProcessInfo]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = raw if isinstance(raw, list) else [raw]
    processes: list[ProcessInfo] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid = safe_int(item.get("PID"))
        if pid <= 0:
            continue
        name = str(item.get("Name") or "")
        command = str(item.get("CommandLine") or name or f"pid-{pid}")
        kernel_time = safe_int(item.get("KernelModeTime"))
        user_time = safe_int(item.get("UserModeTime"))
        owner = str(item.get("Owner") or "?")
        processes.append(
            ProcessInfo(
                pid=pid,
                ppid=max(0, safe_int(item.get("PPID"))),
                user=owner,
                tty="?",
                cpu_seconds=(kernel_time + user_time) / 10_000_000,
                start_time=parse_process_datetime(str(item.get("CreationDate") or "")),
                cmd=command,
                name=name or command_name(command),
                rss=max(0, safe_int(item.get("WorkingSetSize"))),
                virtual_size=max(0, safe_int(item.get("VirtualSize"))),
            )
        )
    return sorted(processes, key=lambda process: process.pid)


def collect_windows_processes_toolhelp() -> list[ProcessInfo]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []
    entry = ProcessEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    processes: list[ProcessInfo] = []
    default_user = getpass.getuser()
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            name = entry.szExeFile or f"pid-{pid}"
            cpu_seconds, start_time = windows_process_times(pid)
            processes.append(
                ProcessInfo(
                    pid=pid,
                    ppid=max(0, int(entry.th32ParentProcessID)),
                    user=default_user,
                    tty="?",
                    cpu_seconds=cpu_seconds,
                    start_time=start_time,
                    cmd=name,
                    name=name,
                )
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return sorted(processes, key=lambda process: process.pid)


def windows_process_times(pid: int) -> tuple[float, _datetime.datetime | None]:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return 0.0, None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return 0.0, None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return 0.0, None
    finally:
        kernel32.CloseHandle(handle)
    cpu_seconds = (filetime_to_int(kernel) + filetime_to_int(user)) / 10_000_000
    return cpu_seconds, datetime_from_filetime(filetime_to_int(creation))


def filetime_to_int(value) -> int:
    return (int(value.dwHighDateTime) << 32) + int(value.dwLowDateTime)


def datetime_from_filetime(value: int) -> _datetime.datetime | None:
    if value <= 0:
        return None
    return _datetime.datetime(1601, 1, 1) + _datetime.timedelta(microseconds=value // 10)


def current_process_info() -> ProcessInfo:
    cpu = sum(os.times()[:2])
    name = Path(sys_argv0()).name or "python"
    return ProcessInfo(
        pid=os.getpid(),
        ppid=0,
        user=getpass.getuser(),
        tty="?",
        cpu_seconds=cpu,
        start_time=None,
        cmd=name,
        name=name,
        state="R",
    )


def filter_current_process_tree(processes: list[ProcessInfo], root_pid: int) -> list[ProcessInfo]:
    by_parent: dict[int, list[ProcessInfo]] = {}
    by_pid = {process.pid: process for process in processes}
    for process in processes:
        by_parent.setdefault(process.ppid, []).append(process)
    selected: dict[int, ProcessInfo] = {}
    if root_pid in by_pid:
        selected[root_pid] = by_pid[root_pid]
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        for child in by_parent.get(pid, []):
            if child.pid in selected:
                continue
            selected[child.pid] = child
            stack.append(child.pid)
    return sorted(selected.values(), key=lambda process: process.pid)


def write_ps_default(stdout: TextIO, processes: list[ProcessInfo], tty: str) -> None:
    rows = [
        [str(process.pid), process.tty if process.tty != "?" else tty, format_cpu_time(process.cpu_seconds), process.name or command_name(process.cmd)]
        for process in processes
    ]
    write_aligned_table(stdout, ["PID", "TTY", "TIME", "CMD"], rows, right_align={"PID", "TIME"})


def write_ps_ef(stdout: TextIO, processes: list[ProcessInfo], tty: str) -> None:
    rows = []
    for process in sorted(processes, key=lambda item: item.pid):
        rows.append(
            [
                process.user or "?",
                str(process.pid),
                str(process.ppid),
                str(int(cpu_percent(process))),
                format_start_time(process.start_time),
                process.tty if process.tty != "?" else tty,
                format_cpu_time(process.cpu_seconds),
                process.cmd or process.name,
            ]
        )
    write_aligned_table(stdout, ["UID", "PID", "PPID", "C", "STIME", "TTY", "TIME", "CMD"], rows, right_align={"PID", "PPID", "C", "TIME"})


def write_ps_aux(stdout: TextIO, processes: list[ProcessInfo], tty: str) -> None:
    total_memory = windows_total_physical_memory()
    rows = []
    for process in sorted(processes, key=lambda item: item.pid):
        rows.append(
            [
                process.user or "?",
                str(process.pid),
                f"{cpu_percent(process):.1f}",
                f"{memory_percent(process.rss, total_memory):.1f}",
                str(max(process.virtual_size, process.rss) // 1024),
                str(process.rss // 1024),
                process.tty if process.tty != "?" else tty,
                process.state or "S",
                format_start_time(process.start_time),
                format_cpu_time(process.cpu_seconds),
                process.cmd or process.name,
            ]
        )
    write_aligned_table(
        stdout,
        ["USER", "PID", "%CPU", "%MEM", "VSZ", "RSS", "TTY", "STAT", "START", "TIME", "COMMAND"],
        rows,
        right_align={"PID", "%CPU", "%MEM", "VSZ", "RSS", "TIME"},
    )


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def command_name(command: str) -> str:
    command = command.strip()
    if not command:
        return ""
    try:
        return Path(shlex.split(command, posix=False)[0].strip('"')).name
    except (ValueError, IndexError):
        return Path(command.split()[0].strip('"')).name if command.split() else command


def parse_process_datetime(value: str) -> _datetime.datetime | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("/Date("):
        match = re.search(r"-?\d+", value)
        if match:
            return _datetime.datetime.fromtimestamp(int(match.group()) / 1000)
    if re.fullmatch(r"\d{14}\.\d{6}[+-]\d{3}", value):
        value = value[:26]
        try:
            return _datetime.datetime.strptime(value, "%Y%m%d%H%M%S.%f")
        except ValueError:
            return None
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def format_cpu_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_start_time(value: _datetime.datetime | None) -> str:
    if value is None:
        return "-"
    now = _datetime.datetime.now()
    if value.date() == now.date():
        return value.strftime("%H:%M")
    return value.strftime("%b%d")


def cpu_percent(process: ProcessInfo) -> float:
    if process.start_time is None:
        return 0.0
    elapsed = max((_datetime.datetime.now() - process.start_time).total_seconds(), 1.0)
    return min(999.9, max(0.0, process.cpu_seconds / elapsed / max(os.cpu_count() or 1, 1) * 100))


def memory_percent(rss: int, total_memory: int) -> float:
    if total_memory <= 0:
        return 0.0
    return max(0.0, rss / total_memory * 100)


def windows_total_physical_memory() -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes
    except ImportError:
        return 0

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return int(status.ullTotalPhys)


def which_command_matches(shell, name: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    if not name:
        return matches
    if shell.is_builtin(name):
        matches.append(("builtin", name))
    elif shell.should_run_internal_utility(name, shell.env):
        matches.append(("utility", name))

    seen_paths: set[str] = set()
    resolved = shell.resolve_command(name, shell.env)
    for path in ([resolved] if resolved else []) + find_executable_matches(name, shell.env):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen_paths:
            continue
        seen_paths.add(key)
        matches.append(("path", path))
    return matches


def find_executable_matches(name: str, env: dict[str, str]) -> list[str]:
    if any(sep in name for sep in ("/", "\\")):
        return [path for path in executable_path_candidates(name, env) if is_executable_file(path)]

    matches: list[str] = []
    path_text = env.get("PATH", os.environ.get("PATH", ""))
    for directory in path_text.split(os.pathsep):
        search_dir = directory or "."
        for candidate in executable_name_candidates(name, env):
            path = os.path.join(search_dir, candidate)
            if is_executable_file(path):
                matches.append(os.path.abspath(path))
    return matches


def executable_path_candidates(path: str, env: dict[str, str]) -> list[str]:
    root, ext = os.path.splitext(path)
    if ext or os.name != "nt":
        return [path]
    return [root + suffix for suffix in windows_pathext(env)]


def executable_name_candidates(name: str, env: dict[str, str]) -> list[str]:
    root, ext = os.path.splitext(name)
    if ext or os.name != "nt":
        return [name]
    return [name + suffix for suffix in windows_pathext(env)]


def windows_pathext(env: dict[str, str]) -> list[str]:
    value = env.get("PATHEXT", os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"))
    suffixes = [suffix if suffix.startswith(".") else "." + suffix for suffix in value.split(";") if suffix]
    return suffixes or [".COM", ".EXE", ".BAT", ".CMD"]


def is_executable_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    if os.name == "nt":
        return True
    return os.access(path, os.X_OK)


LSBLK_COLUMN_NAMES = {
    "NAME",
    "KNAME",
    "MAJ:MIN",
    "RM",
    "SIZE",
    "RO",
    "TYPE",
    "MOUNTPOINT",
    "MOUNTPOINTS",
    "FSTYPE",
    "LABEL",
    "UUID",
    "FSAVAIL",
    "FSUSE%",
    "MODEL",
}


def collect_storage_volumes(paths: list[str] | None = None) -> list[StorageVolume]:
    if paths:
        return [collect_storage_volume_for_path(path) for path in paths]
    if os.name == "nt":
        volumes = collect_windows_storage_volumes()
        if volumes:
            return volumes
    volumes = collect_posix_storage_volumes()
    if volumes:
        return volumes
    return [collect_storage_volume_for_path(os.getcwd())]


def collect_storage_volume_for_path(path: str) -> StorageVolume:
    if not os.path.exists(path):
        raise FileNotFoundError(2, "No such file or directory", path)
    if os.name == "nt":
        return windows_storage_volume_from_root(windows_root_for_path(path))
    mountpoint = find_mountpoint(path)
    usage = shutil.disk_usage(mountpoint)
    device, fstype = posix_mount_metadata(mountpoint)
    return StorageVolume(
        device=device or mountpoint,
        mountpoint=mountpoint,
        fstype=fstype,
        label="",
        total=usage.total,
        used=usage.used,
        free=usage.free,
    )


def collect_windows_storage_volumes() -> list[StorageVolume]:
    volumes: list[StorageVolume] = []
    for root in windows_logical_drive_roots():
        try:
            volumes.append(windows_storage_volume_from_root(root))
        except OSError:
            continue
    return sorted(volumes, key=lambda volume: volume.mountpoint.lower())


def windows_logical_drive_roots() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import ctypes
    except ImportError:
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [f"{chr(ord('A') + index)}:\\" for index in range(26) if mask & (1 << index)]


def windows_storage_volume_from_root(root: str) -> StorageVolume:
    usage = shutil.disk_usage(root)
    fstype, label, serial, readonly = windows_volume_metadata(root)
    drive_type, removable = windows_drive_type(root)
    return StorageVolume(
        device=root.rstrip("\\/") or root,
        mountpoint=root,
        fstype=fstype,
        label=label,
        total=usage.total,
        used=usage.used,
        free=usage.free,
        drive_type=drive_type,
        removable=removable,
        readonly=readonly,
        serial=serial,
    )


def windows_root_for_path(path: str) -> str:
    absolute = os.path.abspath(path)
    anchor = Path(absolute).anchor
    if anchor:
        return anchor
    drive, _tail = os.path.splitdrive(absolute)
    return drive + "\\" if drive else absolute


def windows_volume_metadata(root: str) -> tuple[str, str, str, bool]:
    if os.name != "nt":
        return "", "", "", False
    try:
        import ctypes
    except ImportError:
        return "", "", "", False
    kernel32 = ctypes.windll.kernel32
    label_buffer = ctypes.create_unicode_buffer(261)
    fs_buffer = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    max_component = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    ok = kernel32.GetVolumeInformationW(
        root,
        label_buffer,
        len(label_buffer),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        fs_buffer,
        len(fs_buffer),
    )
    if not ok:
        return "", "", "", False
    readonly = bool(flags.value & 0x00080000)
    return fs_buffer.value, label_buffer.value, f"{serial.value:08X}", readonly


def windows_drive_type(root: str) -> tuple[str, bool]:
    if os.name != "nt":
        return "disk", False
    try:
        import ctypes
    except ImportError:
        return "disk", False
    code = ctypes.windll.kernel32.GetDriveTypeW(root)
    names = {
        0: "unknown",
        1: "unknown",
        2: "removable",
        3: "fixed",
        4: "network",
        5: "rom",
        6: "ram",
    }
    return names.get(code, "unknown"), code in {2, 5}


def collect_posix_storage_volumes() -> list[StorageVolume]:
    mounts_path = Path("/proc/self/mounts")
    if not mounts_path.exists():
        mounts_path = Path("/proc/mounts")
    if not mounts_path.exists():
        return []
    volumes: list[StorageVolume] = []
    seen: set[str] = set()
    try:
        lines = mounts_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device = unescape_mount_field(parts[0])
        mountpoint = unescape_mount_field(parts[1])
        fstype = unescape_mount_field(parts[2])
        if mountpoint in seen:
            continue
        try:
            usage = shutil.disk_usage(mountpoint)
        except OSError:
            continue
        seen.add(mountpoint)
        volumes.append(StorageVolume(device, mountpoint, fstype, "", usage.total, usage.used, usage.free))
    return sorted(volumes, key=lambda volume: volume.mountpoint)


def posix_mount_metadata(mountpoint: str) -> tuple[str, str]:
    for volume in collect_posix_storage_volumes():
        if volume.mountpoint == mountpoint:
            return volume.device, volume.fstype
    return mountpoint, ""


def unescape_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def find_mountpoint(path: str) -> str:
    current = os.path.abspath(path)
    if not os.path.isdir(current):
        current = os.path.dirname(current)
    current = current or os.path.abspath(os.sep)
    try:
        current_device = os.stat(current).st_dev
    except OSError:
        return current
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return current
        try:
            if os.stat(parent).st_dev != current_device:
                return current
        except OSError:
            return current
        current = parent


def format_df_row(volume: StorageVolume, *, human: bool, show_type: bool, block_size: int) -> list[str]:
    row = [volume.device]
    if show_type:
        row.append(volume.fstype or "-")
    if human:
        row.extend(
            [
                format_human_size(volume.total),
                format_human_size(volume.used),
                format_human_size(volume.free),
                format_use_percent(volume.used, volume.total),
                volume.mountpoint,
            ]
        )
    else:
        row.extend(
            [
                format_block_count(volume.total, block_size),
                format_block_count(volume.used, block_size),
                format_block_count(volume.free, block_size),
                format_use_percent(volume.used, volume.total),
                volume.mountpoint,
            ]
        )
    return row


def format_block_count(value: int, block_size: int) -> str:
    return str((max(value, 0) + block_size - 1) // block_size)


def format_use_percent(used: int, total: int) -> str:
    if total <= 0:
        return "-"
    return f"{(max(used, 0) * 100 + total - 1) // total}%"


def format_human_size(value: int) -> str:
    units = ("B", "K", "M", "G", "T", "P", "E")
    amount = float(max(value, 0))
    unit = 0
    while amount >= 1024 and unit + 1 < len(units):
        amount /= 1024
        unit += 1
    if unit == 0:
        return f"{int(amount)}B"
    if amount >= 10 or amount.is_integer():
        return f"{amount:.0f}{units[unit]}"
    return f"{amount:.1f}{units[unit]}"


def parse_size_argument(value: str) -> int | None:
    match = re.fullmatch(r"(?i)(\d+)?([kmgtpe]?)(i?b?)?", value.strip())
    if not match:
        return None
    number_text, suffix, _unit = match.groups()
    number = int(number_text) if number_text else 1
    multipliers = {
        "": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
        "t": 1024**4,
        "p": 1024**5,
        "e": 1024**6,
    }
    return number * multipliers[suffix.lower()]


def parse_lsblk_columns(value: str) -> list[str]:
    columns = []
    for item in value.split(","):
        column = item.strip().upper()
        if column == "MOUNTPOINT":
            column = "MOUNTPOINTS"
        if column:
            columns.append(column)
    return columns


def build_lsblk_rows(
    volumes: list[StorageVolume],
    columns: list[str],
    *,
    bytes_mode: bool,
    no_deps: bool,
    ascii_tree: bool,
) -> list[list[str]]:
    rows: list[list[str]] = []
    branch = "`-" if ascii_tree else "\u2514\u2500"
    for index, volume in enumerate(volumes):
        disk_name = linux_disk_name(index)
        disk_minor = index * 16
        if no_deps:
            rows.append(format_lsblk_row(columns, volume, disk_name, disk_name, disk_minor, "disk", bytes_mode, mountpoint=""))
            continue
        parent = StorageVolume(
            device=volume.device,
            mountpoint="",
            fstype="",
            label="",
            total=volume.total,
            used=volume.used,
            free=volume.free,
            drive_type=volume.drive_type,
            removable=volume.removable,
            readonly=volume.readonly,
            serial="",
        )
        rows.append(format_lsblk_row(columns, parent, disk_name, disk_name, disk_minor, "disk", bytes_mode, mountpoint=""))
        part_name = f"{disk_name}1"
        rows.append(
            format_lsblk_row(
                columns,
                volume,
                branch + part_name,
                part_name,
                disk_minor + 1,
                "part",
                bytes_mode,
                mountpoint=volume.mountpoint,
            )
        )
    return rows


def format_lsblk_row(
    columns: list[str],
    volume: StorageVolume,
    name: str,
    kname: str,
    minor: int,
    row_type: str,
    bytes_mode: bool,
    *,
    mountpoint: str,
) -> list[str]:
    values = {
        "NAME": name,
        "KNAME": kname,
        "MAJ:MIN": f"8:{minor}",
        "RM": "1" if volume.removable else "0",
        "SIZE": str(volume.total) if bytes_mode else format_human_size(volume.total),
        "RO": "1" if volume.readonly else "0",
        "TYPE": row_type,
        "MOUNTPOINT": mountpoint,
        "MOUNTPOINTS": mountpoint,
        "FSTYPE": volume.fstype,
        "LABEL": volume.label,
        "UUID": volume.serial,
        "FSAVAIL": str(volume.free) if bytes_mode else format_human_size(volume.free),
        "FSUSE%": format_use_percent(volume.used, volume.total),
        "MODEL": volume.drive_type,
    }
    return [values[column] for column in columns]


def linux_disk_name(index: int) -> str:
    letters = ""
    value = index
    while True:
        letters = chr(ord("a") + (value % 26)) + letters
        value = value // 26 - 1
        if value < 0:
            break
    return "sd" + letters


def write_aligned_table(
    stdout: TextIO,
    headers: list[str],
    rows: list[list[str]],
    *,
    right_align: set[str] | None = None,
) -> None:
    if not rows and not headers:
        return
    right_align = right_align or set()
    table = ([headers] if headers else []) + rows
    columns = max(len(row) for row in table)
    widths = [0] * columns
    for row in table:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    right_indexes = {index for index, header in enumerate(headers) if header in right_align}
    for row_number, row in enumerate(table):
        parts = []
        for index in range(columns):
            cell = row[index] if index < len(row) else ""
            if row_number > 0 and index in right_indexes:
                parts.append(cell.rjust(widths[index]))
            else:
                parts.append(cell.ljust(widths[index]))
        stdout.write(" ".join(parts).rstrip() + "\n")


HELP_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("!", "! PIPELINE", "Execute PIPELINE and invert its exit status."),
    ("pipeline", "COMMAND1 | COMMAND2", "Connect stdout of COMMAND1 to stdin of COMMAND2."),
    ("list", "COMMAND1 ; COMMAND2", "Run commands sequentially."),
    ("and-or", "COMMAND1 && COMMAND2  /  COMMAND1 || COMMAND2", "Run commands conditionally by exit status."),
    ("if", "if COMMANDS; then COMMANDS; [elif COMMANDS; then COMMANDS;] [else COMMANDS;] fi", "Conditional command execution."),
    ("for", "for NAME [in WORDS ...]; do COMMANDS; done", "Loop over words or positional parameters."),
    ("while", "while COMMANDS; do COMMANDS; done", "Loop while COMMANDS returns success."),
    ("until", "until COMMANDS; do COMMANDS; done", "Loop until COMMANDS returns success."),
    ("case", "case WORD in [PATTERN) COMMANDS ;;]... esac", "Pattern-based branching."),
    ("function", "name() { COMMANDS; }", "Define a POSIX-style shell function."),
    ("group", "{ COMMANDS; }", "Run COMMANDS in the current shell environment."),
    ("subshell", "( COMMANDS )", "Run COMMANDS in a subshell environment."),
    ("redirection", "[n] < > >> <> 2> 2>> >& <& WORD", "Redirect command input, output, or file descriptors."),
    (".", ". filename [arguments]", "Read and execute commands from filename in the current shell."),
    ("source", "source filename [arguments]", "Alias for the dot command."),
    (":", ":", "Return success without doing anything."),
    ("alias", "alias [name[=value] ...]", "Define or display command aliases."),
    ("unalias", "unalias name ...", "Remove command aliases."),
    ("cd", "cd [dir]", "Change the current directory."),
    ("pwd", "pwd", "Print the current directory."),
    ("echo", "echo [-n] [arg ...]", "Write arguments separated by spaces."),
    ("printf", "printf format [arguments]", "Format and print arguments."),
    ("read", "read [-r] [name ...]", "Read one input line into shell variables."),
    ("eval", "eval [arg ...]", "Read arguments as shell input and execute them."),
    ("exec", "exec [command [arg ...]]", "Replace the shell with command, or apply redirections."),
    ("exit", "exit [n]", "Exit the shell with status n."),
    ("return", "return [n]", "Return from a function or sourced script."),
    ("break", "break [n]", "Exit one or more loops."),
    ("continue", "continue [n]", "Continue one or more loops."),
    ("export", "export [name[=value] ...] or export -p", "Mark variables for child process environments."),
    ("readonly", "readonly [name[=value] ...] or readonly -p", "Mark variables as read-only."),
    ("unset", "unset name ...", "Unset shell variables and environment variables."),
    ("set", "set [-efu] [+efu] [-- arg ...]", "Set shell options or positional parameters."),
    ("shift", "shift [n]", "Shift positional parameters."),
    ("getopts", "getopts optstring name [arg ...]", "Parse shell option arguments."),
    ("type", "type name ...", "Describe how each name would be interpreted."),
    ("command", "command [-Vv] command [arg ...]", "Run a command while bypassing shell functions."),
    ("env", "env [-i] [name=value ...] [command [arg ...]]", "Display or run with a modified environment."),
    ("test", "test expr", "Evaluate a conditional expression."),
    ("[", "[ expr ]", "Evaluate a conditional expression."),
    ("trap", "trap [-lp] [[action] signal_spec ...]", "Display, set, or reset signal traps."),
    ("umask", "umask [mode]", "Display or set the file creation mask."),
    ("times", "times", "Display process times."),
    ("hash", "hash [-r] [name ...]", "Remember or display command locations."),
    ("history", "history [-c] [-d offset] [n]", "Display or edit the current interactive command history."),
    ("help", "help [name ...]", "Display this help text."),
    ("clear", "clear", "Clear the terminal using an ANSI fallback sequence."),
    ("base64", "base64 [-d|-D] [file ...]", "Encode or decode base64 data."),
    ("cat", "cat [file ...]", "Concatenate files to standard output."),
    ("cp", "cp [-Rfp] source ... target", "Copy files and directories."),
    ("cut", "cut -b|-c|-f list [file ...]", "Select byte, character, or field ranges."),
    ("date", "date [+format]", "Display the current date and time."),
    ("df", "df [-hkmPT] [file ...]", "Report filesystem disk space usage."),
    ("diff", "diff [-u] file1 file2", "Compare files line by line."),
    ("find", "find [path ...] [expression]", "Walk file trees and match paths."),
    ("grep", "grep [-EinvcqFlHh] pattern [file ...]", "Search files for matching lines."),
    ("install", "install [-D] [-d] [-m mode] source target", "Copy files and set attributes."),
    ("ls", "ls [-Aald1] [file ...]", "List directory contents."),
    ("lsblk", "lsblk [-bdfin] [-o columns]", "List block devices and mounted filesystems."),
    ("mkdir", "mkdir [-p] dir ...", "Create directories."),
    ("mv", "mv source ... target", "Move or rename files."),
    ("ps", "ps [aux|-ef]", "Display Windows process status using Linux-style output."),
    ("rm", "rm [-fRr] file ...", "Remove files or directories."),
    ("sed", "sed [-n] [-e script] [script] [file ...]", "Run a stream editing script."),
    ("sort", "sort [-fnru] [-o file] [file ...]", "Sort text lines."),
    ("tar", "tar -cf|-xf|-tf archive [file ...]", "Create, extract, or list tar archives."),
    ("tr", "tr [-d] set1 [set2]", "Translate or delete characters."),
    ("vi", "vi [file]", "Edit a file with the Windows-only full-screen TTY fallback when vi/vim is missing."),
    ("wc", "wc [-lwc] [file ...]", "Count lines, words, and bytes."),
    ("which", "which [-a] name ...", "Locate commands, builtins, and Windows executable files."),
    ("xargs", "xargs [-0] [-n count] [-I repl] [command ...]", "Build command lines from standard input."),
)

HELP_SUMMARY_ITEMS = tuple(item[1] for item in HELP_ITEMS)
HELP_DETAILS = {name: f"{usage}\n    {description}" for name, usage, description in HELP_ITEMS}


def write_help_table(stdout: TextIO, items: tuple[str, ...]) -> None:
    width = 48
    for index in range(0, len(items), 2):
        left = items[index]
        right = items[index + 1] if index + 1 < len(items) else ""
        if len(left) >= width:
            stdout.write(left + "\n")
            if right:
                stdout.write(f"{'':<{width}}{right}\n")
        else:
            stdout.write(f"{left:<{width}}{right}\n")


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
    "df": utility_df,
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
    "help": utility_help,
    "id": utility_id,
    "install": utility_install,
    "kill": utility_kill,
    "ln": utility_ln,
    "locate": utility_locate,
    "ls": utility_ls,
    "lsblk": utility_lsblk,
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
    "vi": utility_vi,
    "wc": utility_wc,
    "which": utility_which,
    "whoami": utility_whoami,
    "xargs": utility_xargs,
    "yes": utility_yes,
}
