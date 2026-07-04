"""Shell runtime."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Iterator, TextIO

from .builtins import BUILTINS, SPECIAL_BUILTINS
from .errors import ExpansionError, LexerError, ParseError, ShellExit
from .expansion import expand_assignment, expand_redirection, expand_word
from .parser import AndOrList, ListItem, Pipeline, Redirection, Script, SimpleCommand, parse


@dataclass
class ExpandedCommand:
    assignments: dict[str, str]
    argv: list[str]
    redirections: list[tuple[int, str, str]]


class Shell:
    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        argv0: str = "pysh",
        positional: list[str] | None = None,
        interactive: bool = False,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.vars: dict[str, str] = {}
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.argv0 = argv0
        self.positional = list(positional or [])
        self.interactive = interactive
        self.last_status = 0
        self._last_external_output = ""
        self.vars["IFS"] = self.env.pop("IFS", " \t\n")
        self.env.setdefault("PWD", os.getcwd())
        if "HOME" not in self.env:
            home = os.path.expanduser("~")
            if home and home != "~":
                self.env["HOME"] = home

    def clone(self, *, stdout: TextIO | None = None) -> "Shell":
        child = Shell(
            env=self.env.copy(),
            stdin=self.stdin,
            stdout=stdout if stdout is not None else self.stdout,
            stderr=self.stderr,
            argv0=self.argv0,
            positional=self.positional.copy(),
            interactive=False,
        )
        child.vars = self.vars.copy()
        child.last_status = self.last_status
        return child

    def execute(self, source: str) -> int:
        try:
            script = parse(source)
            status = self.execute_script(script)
        except (LexerError, ParseError, ExpansionError) as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 2
        except ShellExit as exc:
            raise exc
        self.last_status = status
        return status

    def execute_script(self, script: Script) -> int:
        status = 0
        for item in script.items:
            status = self.execute_list_item(item)
            self.last_status = status
        return status

    def execute_list_item(self, item: ListItem) -> int:
        if not item.background:
            return self.execute_and_or(item.command)

        child = self.clone()
        thread = threading.Thread(target=child.execute_and_or, args=(item.command,), daemon=True)
        thread.start()
        return 0

    def execute_and_or(self, item: AndOrList) -> int:
        status = self.execute_pipeline(item.first)
        for op, pipeline in item.rest:
            if op == "&&" and status != 0:
                continue
            if op == "||" and status == 0:
                continue
            status = self.execute_pipeline(pipeline)
        self.last_status = status
        return status

    def execute_pipeline(self, pipeline: Pipeline) -> int:
        if len(pipeline.commands) == 1:
            status, _output = self.execute_simple(pipeline.commands[0])
            return status

        input_text: str | None = None
        status = 0
        for index, command in enumerate(pipeline.commands):
            capture_stdout = index < len(pipeline.commands) - 1
            status, output = self.execute_simple(
                command,
                pipeline_input=input_text,
                capture_stdout=capture_stdout,
                in_pipeline=True,
            )
            input_text = output if capture_stdout else None
        self.last_status = status
        return status

    def execute_simple(
        self,
        command: SimpleCommand,
        *,
        pipeline_input: str | None = None,
        capture_stdout: bool = False,
        in_pipeline: bool = False,
    ) -> tuple[int, str]:
        try:
            expanded = self.expand_command(command)
        except ExpansionError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            return 2, ""

        if not expanded.argv:
            for name, value in expanded.assignments.items():
                self.set_parameter(name, value)
            try:
                with self.redirected(expanded.redirections, self.stdin, self.stdout, self.stderr):
                    pass
            except OSError as exc:
                self.stderr.write(f"{self.argv0}: {exc}\n")
                return 1, ""
            return 0, ""

        name = expanded.argv[0]
        if name in BUILTINS:
            return self.run_builtin_command(
                name,
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
                in_pipeline=in_pipeline,
            )
        return self.run_external_command(
            expanded,
            pipeline_input=pipeline_input,
            capture_stdout=capture_stdout,
        )

    def expand_command(self, command: SimpleCommand) -> ExpandedCommand:
        assignments: dict[str, str] = {}
        for name, word in command.assignments:
            assignments[name] = expand_assignment(self, word)

        argv: list[str] = []
        for word in command.words:
            argv.extend(expand_word(self, word))

        redirections: list[tuple[int, str, str]] = []
        for redir in command.redirections:
            if redir.op == "<<":
                raise ExpansionError("here-documents are not implemented")
            redirections.append((redir.fd, redir.op, expand_redirection(self, redir.target)))
        return ExpandedCommand(assignments, argv, redirections)

    def run_builtin_command(
        self,
        name: str,
        expanded: ExpandedCommand,
        *,
        pipeline_input: str | None,
        capture_stdout: bool,
        in_pipeline: bool,
    ) -> tuple[int, str]:
        stdin = io.StringIO(pipeline_input) if pipeline_input is not None else self.stdin
        stdout_buffer = io.StringIO() if capture_stdout else None
        stdout = stdout_buffer if stdout_buffer is not None else self.stdout
        restore: dict[str, tuple[bool, str, bool]] = {}
        persist_assignments = (name in SPECIAL_BUILTINS) and not in_pipeline

        if expanded.assignments and not persist_assignments:
            for var_name, value in expanded.assignments.items():
                restore[var_name] = (
                    self.parameter_is_set(var_name),
                    self.get_parameter(var_name),
                    var_name in self.env,
                )
                self.set_parameter(var_name, value)
        else:
            for var_name, value in expanded.assignments.items():
                self.set_parameter(var_name, value, export=var_name in self.env)

        try:
            with self.redirected(expanded.redirections, stdin, stdout, self.stderr) as streams:
                status = BUILTINS[name](self, expanded.argv, streams[0], streams[1], streams[2])
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 1
        finally:
            for var_name, (was_set, old_value, was_exported) in restore.items():
                if was_set:
                    self.set_parameter(var_name, old_value, export=was_exported)
                else:
                    self.unset_parameter(var_name)

        return status, stdout_buffer.getvalue() if stdout_buffer is not None else ""

    def run_external_command(
        self,
        expanded: ExpandedCommand,
        *,
        pipeline_input: str | None = None,
        capture_stdout: bool = False,
    ) -> tuple[int, str]:
        env = dict(self.env)
        env.update(expanded.assignments)
        stdout_redirected = any(fd == 1 for fd, _op, _target in expanded.redirections)
        effective_capture = capture_stdout and not stdout_redirected
        try:
            with self.redirected(expanded.redirections, self.stdin, self.stdout, self.stderr) as streams:
                status = self.run_external(
                    expanded.argv,
                    env=env,
                    stdin=streams[0],
                    stdout=streams[1],
                    stderr=streams[2],
                    input_text=pipeline_input,
                    capture_stdout=effective_capture,
                )
                return status, self._last_external_output if effective_capture else ""
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            return 1, ""

    def run_external(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        input_text: str | None = None,
        capture_stdout: bool = False,
    ) -> int:
        if not argv:
            return 0
        env = dict(self.env if env is None else env)
        executable = self.resolve_command(argv[0], env)
        if executable is None:
            (stderr or self.stderr).write(f"{argv[0]}: command not found\n")
            return 127

        stdin = stdin if stdin is not None else self.stdin
        stdout = stdout if stdout is not None else self.stdout
        stderr = stderr if stderr is not None else self.stderr

        popen_stdin: object = stdin
        popen_stdout: object = stdout
        popen_stderr: object = stderr
        communicate_input = input_text

        if input_text is not None:
            popen_stdin = None
        elif not has_fileno(stdin):
            communicate_input = stdin.read()
            popen_stdin = None

        stdout_capture_target: TextIO | None = None
        if capture_stdout:
            popen_stdout = subprocess.PIPE
        elif not has_fileno(stdout):
            stdout_capture_target = stdout
            popen_stdout = subprocess.PIPE

        stderr_capture_target: TextIO | None = None
        if not has_fileno(stderr):
            stderr_capture_target = stderr
            popen_stderr = subprocess.PIPE

        try:
            completed = subprocess.run(
                [executable, *argv[1:]],
                input=communicate_input,
                stdin=popen_stdin,
                stdout=popen_stdout,
                stderr=popen_stderr,
                env=env,
                text=True,
            )
        except PermissionError:
            (stderr or self.stderr).write(f"{argv[0]}: permission denied\n")
            return 126
        except FileNotFoundError:
            (stderr or self.stderr).write(f"{argv[0]}: command not found\n")
            return 127
        except OSError as exc:
            (stderr or self.stderr).write(f"{argv[0]}: {exc}\n")
            return 126

        if capture_stdout and completed.stdout:
            self._last_external_output = completed.stdout
        else:
            self._last_external_output = ""
        if stdout_capture_target is not None and completed.stdout:
            stdout_capture_target.write(completed.stdout)
        if stderr_capture_target is not None and completed.stderr:
            stderr_capture_target.write(completed.stderr)
        return completed.returncode

    def run_preexpanded(self, argv: list[str], *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
        if not argv:
            return 0
        if argv[0] in BUILTINS and argv[0] != "command":
            return BUILTINS[argv[0]](self, argv, stdin, stdout, stderr)
        return self.run_external(argv, stdin=stdin, stdout=stdout, stderr=stderr)

    @contextlib.contextmanager
    def redirected(
        self,
        redirections: list[tuple[int, str, str]],
        stdin: TextIO,
        stdout: TextIO,
        stderr: TextIO,
    ) -> Iterator[dict[int, TextIO]]:
        streams: dict[int, TextIO] = {0: stdin, 1: stdout, 2: stderr}
        opened: list[TextIO] = []
        try:
            for fd, op, target in redirections:
                if op in {"<", "<&"} and op == "<":
                    file = open(target, "r", encoding="utf-8", errors="replace")
                    opened.append(file)
                    streams[fd] = file
                elif op in {">", ">|"}:
                    file = open(target, "w", encoding="utf-8")
                    opened.append(file)
                    streams[fd] = file
                elif op == ">>":
                    file = open(target, "a", encoding="utf-8")
                    opened.append(file)
                    streams[fd] = file
                elif op in {">&", "<&"}:
                    if target == "-":
                        file = open(os.devnull, "w" if fd != 0 else "r", encoding="utf-8")
                        opened.append(file)
                        streams[fd] = file
                    elif target.isdigit() and int(target) in streams:
                        streams[fd] = streams[int(target)]
                    else:
                        raise OSError(f"bad file descriptor: {target}")
                else:
                    raise OSError(f"unsupported redirection: {op}")
            yield streams
        finally:
            for file in opened:
                file.close()

    def capture(self, source: str) -> str:
        stdout = io.StringIO()
        child = self.clone(stdout=stdout)
        try:
            child.execute(source)
        except ShellExit:
            pass
        return stdout.getvalue()

    def repl(self) -> int:
        status = 0
        buffer = ""
        while True:
            prompt = self.get_parameter("PS2") if buffer else self.get_parameter("PS1") or "$ "
            try:
                line = input(prompt)
            except EOFError:
                self.stdout.write("\n")
                return status
            candidate = buffer + line + "\n"
            try:
                parse(candidate)
            except LexerError as exc:
                if "unterminated" in str(exc):
                    buffer = candidate
                    continue
                self.stderr.write(f"{self.argv0}: {exc}\n")
                buffer = ""
                status = 2
                continue
            try:
                status = self.execute(candidate)
            except ShellExit as exc:
                return exc.status
            buffer = ""

    def get_parameter(self, name: str) -> str:
        if name == "?":
            return str(self.last_status)
        if name == "$":
            return str(os.getpid())
        if name == "#":
            return str(len(self.positional))
        if name == "0":
            return self.argv0
        if name == "@":
            return " ".join(self.positional)
        if name == "*":
            sep = (self.get_parameter("IFS") or " ")[0]
            return sep.join(self.positional)
        if len(name) == 1 and name.isdigit():
            index = int(name) - 1
            return self.positional[index] if 0 <= index < len(self.positional) else ""
        if name in self.vars:
            return self.vars[name]
        return self.env.get(name, "")

    def parameter_is_set(self, name: str) -> bool:
        if name in {"?", "$", "#", "0", "@", "*"}:
            return True
        if len(name) == 1 and name.isdigit():
            return 0 <= int(name) - 1 < len(self.positional)
        return name in self.vars or name in self.env

    def set_parameter(self, name: str, value: str, *, export: bool = False) -> None:
        self.vars[name] = value
        if export or name in self.env:
            self.env[name] = value

    def unset_parameter(self, name: str) -> None:
        self.vars.pop(name, None)
        self.env.pop(name, None)

    def is_builtin(self, name: str) -> bool:
        return name in BUILTINS

    def which(self, name: str) -> str | None:
        return self.resolve_command(name, self.env)

    def resolve_command(self, name: str, env: dict[str, str]) -> str | None:
        if any(sep in name for sep in ("/", "\\")):
            return name if os.path.exists(name) else None
        return shutil.which(name, path=env.get("PATH"))


def has_fileno(stream: TextIO) -> bool:
    try:
        stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return False
    return True
