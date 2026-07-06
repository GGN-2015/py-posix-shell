"""Shell runtime."""

from __future__ import annotations

import contextlib
import fnmatch
import io
import os
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, TextIO

from .builtins import BUILTINS, SPECIAL_BUILTINS
from .errors import ExpansionError, LexerError, ParseError, ShellBreak, ShellContinue, ShellExit, ShellReturn
from .expansion import chars_to_text, expand_assignment, expand_here_document, expand_redirection, expand_text, expand_word, expand_word_to_chars
from .lexer import Operator, Word, lex
from .parser import (
    AndOrList,
    CaseCommand,
    Command,
    ForCommand,
    FunctionDef,
    GroupCommand,
    IfCommand,
    ListItem,
    LoopCommand,
    Pipeline,
    Redirection,
    Script,
    SimpleCommand,
    SubshellCommand,
    parse,
)
from .posix_utils import INTERNAL_UTILITIES, executable_path_candidates, find_executable_matches, is_executable_file, normalize_path_entry, split_path_list


@dataclass
class ExpandedCommand:
    assignments: dict[str, str]
    argv: list[str]
    redirections: list[tuple[int, str, str]]
    conditional_quoted: tuple[bool, ...] = field(default_factory=tuple)


@dataclass
class HereDoc:
    body: str
    expand: bool


PipelineValue = str | bytes


class PipelineBinaryBuffer:
    def __init__(self, owner: "PipelineCapture") -> None:
        self.owner = owner

    def write(self, data: bytes | bytearray | memoryview) -> int:
        chunk = bytes(data)
        self.owner.write_bytes(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass


class PipelineCapture(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self._binary = False
        self._bytes = bytearray()
        self.buffer = PipelineBinaryBuffer(self)

    def write(self, text: str) -> int:
        if self._binary:
            self._bytes.extend(text.encode("utf-8"))
            return len(text)
        return super().write(text)

    def write_bytes(self, data: bytes) -> None:
        if not self._binary:
            existing = super().getvalue()
            if existing:
                self._bytes.extend(existing.encode("utf-8"))
            super().seek(0)
            super().truncate(0)
            self._binary = True
        self._bytes.extend(data)

    def get_pipeline_value(self) -> PipelineValue:
        if self._binary:
            return bytes(self._bytes)
        return super().getvalue()


@dataclass(frozen=True)
class CompletionResult:
    line: str
    beep: bool = False
    listings: tuple[str, ...] = field(default_factory=tuple)
    hidden_count: int = 0


@dataclass
class LineHistoryState:
    index: int
    saved_line: str = ""


@dataclass
class LocalVariableSnapshot:
    vars_present: bool
    vars_value: str
    env_present: bool
    env_value: str


class Shell:
    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        argv0: str = "pysh",
        shell_path: str | None = None,
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
        self.functions: dict[str, FunctionDef] = {}
        self.history: list[str] = []
        self.here_docs: dict[str, list[HereDoc]] = {}
        self.aliases: dict[str, str] = {}
        self.traps: dict[str, str] = {}
        self.readonly: set[str] = set()
        self.command_hash: dict[str, str] = {}
        self._local_scopes: list[dict[str, LocalVariableSnapshot]] = []
        self._conditional_quoted: tuple[bool, ...] = ()
        self._alias_stack: set[str] = set()
        self._getopts_nextchar = 1
        self._execute_depth = 0
        self._running_exit_trap = False
        self.options = {"errexit": False, "nounset": False, "noglob": False}
        self._suppress_errexit = 0
        self._last_errexit_exempt = False
        self.vars["IFS"] = self.env.pop("IFS", " \t\n")
        self.env.setdefault("PWD", os.getcwd())
        if "HOME" not in self.env:
            home = os.path.expanduser("~")
            if home and home != "~":
                self.env["HOME"] = home
        if not self.env.get("SHELL"):
            self.env["SHELL"] = resolve_shell_environment_value(shell_path or argv0, self.env)
        ensure_cygpath_console_script_on_path(self.env)

    def clone(self, *, stdout: TextIO | None = None) -> "Shell":
        child = Shell(
            env=self.env.copy(),
            stdin=self.stdin,
            stdout=stdout if stdout is not None else self.stdout,
            stderr=self.stderr,
            argv0=self.argv0,
            shell_path=self.env.get("SHELL"),
            positional=self.positional.copy(),
            interactive=False,
        )
        child.vars = self.vars.copy()
        child.functions = self.functions.copy()
        child.history = self.history.copy()
        child.here_docs = {name: docs.copy() for name, docs in self.here_docs.items()}
        child.aliases = self.aliases.copy()
        child.traps = self.traps.copy()
        child.readonly = self.readonly.copy()
        child.command_hash = self.command_hash.copy()
        child._local_scopes = [scope.copy() for scope in self._local_scopes]
        child._conditional_quoted = self._conditional_quoted
        child._alias_stack = self._alias_stack.copy()
        child._getopts_nextchar = self._getopts_nextchar
        child._execute_depth = self._execute_depth
        child._running_exit_trap = self._running_exit_trap
        child.options = self.options.copy()
        child._suppress_errexit = self._suppress_errexit
        child._last_errexit_exempt = self._last_errexit_exempt
        child.last_status = self.last_status
        return child

    def in_function(self) -> bool:
        return bool(self._local_scopes)

    def push_local_scope(self) -> None:
        self._local_scopes.append({})

    def pop_local_scope(self) -> None:
        scope = self._local_scopes.pop()
        for name, snapshot in reversed(scope.items()):
            if snapshot.vars_present:
                self.vars[name] = snapshot.vars_value
            else:
                self.vars.pop(name, None)
            if snapshot.env_present:
                self.env[name] = snapshot.env_value
            else:
                self.env.pop(name, None)

    def declare_local_parameter(self, name: str, value: str | None = None, *, export: bool = False) -> None:
        if not self._local_scopes:
            raise ExpansionError("local: can only be used in a function")
        scope = self._local_scopes[-1]
        first_declaration = name not in scope
        if first_declaration:
            scope[name] = LocalVariableSnapshot(
                name in self.vars,
                self.vars.get(name, ""),
                name in self.env,
                self.env.get(name, ""),
            )
        if value is None:
            if first_declaration:
                self.set_parameter(name, "", export=export)
            elif export:
                self.set_parameter(name, self.get_parameter(name), export=True)
            return
        self.set_parameter(name, value, export=export)

    def startup_file_path(self) -> Path:
        return Path(self.get_parameter("HOME") or os.path.expanduser("~")) / ".pyshrc"

    def source_startup_file(self) -> int:
        path = self.startup_file_path()
        if not path.exists():
            return 0
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {path}: {exc.strerror or exc}\n")
            self.last_status = 1
            return 1
        try:
            status = self.execute(source, allow_return=True)
        except ShellReturn as exc:
            status = exc.status
            self.last_status = status
        return status

    def execute(self, source: str, *, allow_return: bool = False) -> int:
        old_here_docs = self.here_docs
        self._execute_depth += 1
        try:
            source, self.here_docs = prepare_heredocs(source)
            script = parse(source)
            status = self.execute_script(script)
        except (LexerError, ParseError, ExpansionError) as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 2
        except ShellReturn as exc:
            if allow_return:
                raise exc
            self.stderr.write(f"{self.argv0}: return: can only return from a function or sourced script\n")
            status = exc.status or 2
        except ShellBreak:
            self.stderr.write(f"{self.argv0}: break: only meaningful in a loop\n")
            status = 2
        except ShellContinue:
            self.stderr.write(f"{self.argv0}: continue: only meaningful in a loop\n")
            status = 2
        except KeyboardInterrupt:
            status = 130
        except ShellExit as exc:
            self.last_status = exc.status
            if self._execute_depth == 1:
                self.run_exit_trap()
            raise exc
        finally:
            self.here_docs = old_here_docs
            self._execute_depth -= 1
        self.last_status = status
        if self._execute_depth == 0:
            self.run_exit_trap()
        return status

    def execute_script(self, script: Script) -> int:
        status = 0
        for item in script.items:
            status = self.execute_list_item(item)
            self.last_status = status
            if (
                status != 0
                and self.options.get("errexit", False)
                and self._suppress_errexit == 0
                and not self._last_errexit_exempt
            ):
                raise ShellExit(status)
        return status

    def execute_list_item(self, item: ListItem) -> int:
        if not item.background:
            return self.execute_and_or(item.command)

        child = self.clone()
        thread = threading.Thread(target=child.execute_and_or, args=(item.command,), daemon=True)
        thread.start()
        return 0

    def execute_and_or(self, item: AndOrList) -> int:
        self._last_errexit_exempt = False
        with self.suppress_errexit():
            status = self.execute_pipeline(item.first)
        executed_last = not item.rest
        for index, (op, pipeline) in enumerate(item.rest):
            if op == "&&" and status != 0:
                executed_last = False
                continue
            if op == "||" and status == 0:
                executed_last = False
                continue
            is_last = index == len(item.rest) - 1
            executed_last = is_last
            if is_last:
                status = self.execute_pipeline(pipeline)
            else:
                with self.suppress_errexit():
                    status = self.execute_pipeline(pipeline)
        self._last_errexit_exempt = bool(item.rest and not executed_last)
        self.last_status = status
        return status

    def execute_pipeline(self, pipeline: Pipeline) -> int:
        if len(pipeline.commands) == 1:
            status, _output = self.execute_command(pipeline.commands[0])
            return invert_status(status) if pipeline.negated else status

        input_text: PipelineValue | None = None
        status = 0
        for index, command in enumerate(pipeline.commands):
            capture_stdout = index < len(pipeline.commands) - 1
            status, output = self.execute_command(
                command,
                pipeline_input=input_text,
                capture_stdout=capture_stdout,
                in_pipeline=True,
            )
            input_text = output if capture_stdout else None
        if pipeline.negated:
            status = invert_status(status)
        self.last_status = status
        return status

    def execute_command(
        self,
        command: Command,
        *,
        pipeline_input: PipelineValue | None = None,
        capture_stdout: bool = False,
        in_pipeline: bool = False,
    ) -> tuple[int, PipelineValue]:
        if isinstance(command, SimpleCommand):
            return self.execute_simple(
                command,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
                in_pipeline=in_pipeline,
            )
        if isinstance(command, FunctionDef):
            self.functions[command.name] = command
            return 0, ""
        return self.execute_compound(
            command,
            pipeline_input=pipeline_input,
            capture_stdout=capture_stdout,
            in_pipeline=in_pipeline,
        )

    def execute_compound(
        self,
        command: Command,
        *,
        pipeline_input: PipelineValue | None = None,
        capture_stdout: bool = False,
        in_pipeline: bool = False,
    ) -> tuple[int, PipelineValue]:
        redirections = self.expand_redirections(getattr(command, "redirections", ()))
        stdin = pipeline_input_stream(pipeline_input) if pipeline_input is not None else self.stdin
        stdout_buffer = PipelineCapture() if capture_stdout else None
        stdout = stdout_buffer if stdout_buffer is not None else self.stdout
        target = self.clone(stdout=stdout) if in_pipeline or isinstance(command, SubshellCommand) else self

        try:
            with target.redirected(redirections, stdin, stdout, target.stderr) as streams:
                with target.using_streams(streams[0], streams[1], streams[2]):
                    status = target.execute_compound_body(command)
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 1
        return status, stdout_buffer.get_pipeline_value() if stdout_buffer is not None else ""

    def execute_compound_body(self, command: Command) -> int:
        if isinstance(command, IfCommand):
            return self.execute_if(command)
        if isinstance(command, LoopCommand):
            return self.execute_loop(command)
        if isinstance(command, ForCommand):
            return self.execute_for(command)
        if isinstance(command, CaseCommand):
            return self.execute_case(command)
        if isinstance(command, GroupCommand):
            return self.execute_script(command.body)
        if isinstance(command, SubshellCommand):
            return self.execute_script(command.body)
        raise TypeError(f"unsupported compound command: {type(command).__name__}")

    def execute_if(self, command: IfCommand) -> int:
        with self.suppress_errexit():
            condition_status = self.execute_script(command.condition)
        if condition_status == 0:
            return self.execute_script(command.then_body)
        for condition, body in command.elifs:
            with self.suppress_errexit():
                condition_status = self.execute_script(condition)
            if condition_status == 0:
                return self.execute_script(body)
        if command.else_body is not None:
            return self.execute_script(command.else_body)
        return 0

    def execute_loop(self, command: LoopCommand) -> int:
        status = 0
        while True:
            with self.suppress_errexit():
                condition_status = self.execute_script(command.condition)
            should_run = condition_status == 0 if command.kind == "while" else condition_status != 0
            if not should_run:
                break
            try:
                status = self.execute_script(command.body)
            except ShellContinue as exc:
                if exc.levels > 1:
                    raise ShellContinue(exc.levels - 1)
                status = 0
                continue
            except ShellBreak as exc:
                if exc.levels > 1:
                    raise ShellBreak(exc.levels - 1)
                status = 0
                break
        return status

    def execute_for(self, command: ForCommand) -> int:
        values = self.positional.copy()
        if command.words is not None:
            values = []
            for word in command.words:
                values.extend(expand_word(self, word))

        status = 0
        for value in values:
            self.set_parameter(command.name, value)
            try:
                status = self.execute_script(command.body)
            except ShellContinue as exc:
                if exc.levels > 1:
                    raise ShellContinue(exc.levels - 1)
                status = 0
                continue
            except ShellBreak as exc:
                if exc.levels > 1:
                    raise ShellBreak(exc.levels - 1)
                status = 0
                break
        return status

    def execute_case(self, command: CaseCommand) -> int:
        values = expand_word(self, command.word, field_split=False, pathname=False)
        subject = values[0] if values else ""
        for item in command.items:
            for pattern_word in item.patterns:
                patterns = expand_word(self, pattern_word, field_split=False, pathname=False)
                pattern = patterns[0] if patterns else ""
                if fnmatch.fnmatchcase(subject, pattern):
                    return self.execute_script(item.body)
        return 0

    def execute_simple(
        self,
        command: SimpleCommand,
        *,
        pipeline_input: PipelineValue | None = None,
        capture_stdout: bool = False,
        in_pipeline: bool = False,
    ) -> tuple[int, PipelineValue]:
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
        if name in self.aliases and name not in self._alias_stack:
            return self.run_alias_command(
                name,
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
                in_pipeline=in_pipeline,
            )
        if name in BUILTINS and name in SPECIAL_BUILTINS:
            return self.run_builtin_command(
                name,
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
                in_pipeline=in_pipeline,
            )
        if name in self.functions:
            return self.run_function_command(
                name,
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
                in_pipeline=in_pipeline,
            )
        if name in BUILTINS:
            return self.run_builtin_command(
                name,
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
                in_pipeline=in_pipeline,
            )
        if self.should_run_internal_utility(name, dict(self.env, **expanded.assignments)):
            return self.run_internal_utility_command(
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
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
        conditional_quoted: list[bool] = []
        is_conditional = bool(command.words and command.words[0].text == "[[" and not command.words[0].has_quoted_part)
        for word in command.words:
            if is_conditional:
                chars, _had_quoted = expand_word_to_chars(self, word)
                argv.append(chars_to_text(chars))
                conditional_quoted.append(word.has_quoted_part)
            else:
                argv.extend(expand_word(self, word))

        redirections: list[tuple[int, str, str]] = []
        for redir in command.redirections:
            if redir.op in {"<<", "<<-"}:
                redirections.append((redir.fd, redir.op, self.pop_here_doc(redir.target.text)))
            else:
                redirections.append((redir.fd, redir.op, expand_redirection(self, redir.target)))
        return ExpandedCommand(assignments, argv, redirections, tuple(conditional_quoted))

    def expand_redirections(self, redirections: tuple[Redirection, ...]) -> list[tuple[int, str, str]]:
        expanded: list[tuple[int, str, str]] = []
        for redir in redirections:
            if redir.op in {"<<", "<<-"}:
                expanded.append((redir.fd, redir.op, self.pop_here_doc(redir.target.text)))
            else:
                expanded.append((redir.fd, redir.op, expand_redirection(self, redir.target)))
        return expanded

    def pop_here_doc(self, delimiter: str) -> str:
        docs = self.here_docs.get(delimiter)
        if not docs:
            raise ExpansionError(f"missing here-document body for {delimiter}")
        doc = docs.pop(0)
        return expand_here_document(self, doc.body) if doc.expand else doc.body

    def run_builtin_command(
        self,
        name: str,
        expanded: ExpandedCommand,
        *,
        pipeline_input: PipelineValue | None,
        capture_stdout: bool,
        in_pipeline: bool,
    ) -> tuple[int, PipelineValue]:
        stdin = pipeline_input_stream(pipeline_input) if pipeline_input is not None else self.stdin
        stdout_buffer = PipelineCapture() if capture_stdout else None
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
                old_conditional_quoted = self._conditional_quoted
                self._conditional_quoted = expanded.conditional_quoted
                status = BUILTINS[name](self, expanded.argv, streams[0], streams[1], streams[2])
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 1
        except ExpansionError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 2
        finally:
            self._conditional_quoted = old_conditional_quoted if "old_conditional_quoted" in locals() else ()
            for var_name, (was_set, old_value, was_exported) in restore.items():
                if was_set:
                    self.set_parameter(var_name, old_value, export=was_exported)
                else:
                    self.unset_parameter(var_name)

        return status, stdout_buffer.get_pipeline_value() if stdout_buffer is not None else ""

    def run_alias_command(
        self,
        name: str,
        expanded: ExpandedCommand,
        *,
        pipeline_input: PipelineValue | None,
        capture_stdout: bool,
        in_pipeline: bool,
    ) -> tuple[int, PipelineValue]:
        target = self.clone() if in_pipeline else self
        stdin = pipeline_input_stream(pipeline_input) if pipeline_input is not None else target.stdin
        stdout_buffer = PipelineCapture() if capture_stdout else None
        stdout = stdout_buffer if stdout_buffer is not None else target.stdout
        restore: dict[str, tuple[bool, str, bool]] = {}

        for var_name, value in expanded.assignments.items():
            restore[var_name] = (
                target.parameter_is_set(var_name),
                target.get_parameter(var_name),
                var_name in target.env,
            )
            target.set_parameter(var_name, value, export=var_name in target.env)

        source = " ".join([target.aliases[name], *[quote_shell_arg(arg) for arg in expanded.argv[1:]]])
        try:
            with target.redirected(expanded.redirections, stdin, stdout, target.stderr) as streams:
                with target.using_streams(streams[0], streams[1], streams[2]):
                    target._alias_stack.add(name)
                    try:
                        status = target.execute(source, allow_return=True)
                    finally:
                        target._alias_stack.discard(name)
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 1
        finally:
            for var_name, (was_set, old_value, was_exported) in restore.items():
                if was_set:
                    target.set_parameter(var_name, old_value, export=was_exported)
                else:
                    target.unset_parameter(var_name)

        return status, stdout_buffer.get_pipeline_value() if stdout_buffer is not None else ""

    def run_internal_utility_command(
        self,
        expanded: ExpandedCommand,
        *,
        pipeline_input: PipelineValue | None,
        capture_stdout: bool,
    ) -> tuple[int, PipelineValue]:
        name = expanded.argv[0]
        stdin = pipeline_input_stream(pipeline_input) if pipeline_input is not None else self.stdin
        stdout_buffer = PipelineCapture() if capture_stdout else None
        stdout = stdout_buffer if stdout_buffer is not None else self.stdout
        restore_env: dict[str, tuple[bool, str]] = {}

        for var_name, value in expanded.assignments.items():
            restore_env[var_name] = (var_name in self.env, self.env.get(var_name, ""))
            self.env[var_name] = value

        try:
            with self.redirected(expanded.redirections, stdin, stdout, self.stderr) as streams:
                status = INTERNAL_UTILITIES[name](self, expanded.argv, streams[0], streams[1], streams[2])
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 1
        finally:
            for var_name, (was_set, old_value) in restore_env.items():
                if was_set:
                    self.env[var_name] = old_value
                else:
                    self.env.pop(var_name, None)

        return status, stdout_buffer.get_pipeline_value() if stdout_buffer is not None else ""

    def run_function_command(
        self,
        name: str,
        expanded: ExpandedCommand,
        *,
        pipeline_input: PipelineValue | None,
        capture_stdout: bool,
        in_pipeline: bool,
    ) -> tuple[int, PipelineValue]:
        function = self.functions[name]
        target = self.clone() if in_pipeline else self
        stdin = pipeline_input_stream(pipeline_input) if pipeline_input is not None else target.stdin
        stdout_buffer = PipelineCapture() if capture_stdout else None
        stdout = stdout_buffer if stdout_buffer is not None else target.stdout
        restore: dict[str, tuple[bool, str, bool]] = {}

        for var_name, value in expanded.assignments.items():
            restore[var_name] = (
                target.parameter_is_set(var_name),
                target.get_parameter(var_name),
                var_name in target.env,
            )
            target.set_parameter(var_name, value, export=var_name in target.env)

        old_positional = target.positional
        old_argv0 = target.argv0
        target.positional = expanded.argv[1:]
        target.argv0 = name
        redirections = target.expand_redirections(function.redirections) + expanded.redirections
        target.push_local_scope()

        try:
            with target.redirected(redirections, stdin, stdout, target.stderr) as streams:
                with target.using_streams(streams[0], streams[1], streams[2]):
                    try:
                        status, _output = target.execute_command(function.body)
                    except ShellReturn as exc:
                        status = exc.status
        except OSError as exc:
            self.stderr.write(f"{self.argv0}: {exc}\n")
            status = 1
        finally:
            target.pop_local_scope()
            target.positional = old_positional
            target.argv0 = old_argv0
            for var_name, (was_set, old_value, was_exported) in restore.items():
                if was_set:
                    target.set_parameter(var_name, old_value, export=was_exported)
                else:
                    target.unset_parameter(var_name)

        return status, stdout_buffer.get_pipeline_value() if stdout_buffer is not None else ""

    def run_external_command(
        self,
        expanded: ExpandedCommand,
        *,
        pipeline_input: PipelineValue | None = None,
        capture_stdout: bool = False,
    ) -> tuple[int, PipelineValue]:
        env = dict(self.env)
        env.update(expanded.assignments)
        if self.should_run_internal_utility(expanded.argv[0], env):
            return self.run_internal_utility_command(
                expanded,
                pipeline_input=pipeline_input,
                capture_stdout=capture_stdout,
            )
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
        input_text: PipelineValue | None = None,
        capture_stdout: bool = False,
    ) -> int:
        if not argv:
            return 0
        env = dict(self.env if env is None else env)
        if self.should_run_internal_utility(argv[0], env):
            return INTERNAL_UTILITIES[argv[0]](
                self,
                argv,
                stdin if stdin is not None else self.stdin,
                stdout if stdout is not None else self.stdout,
                stderr if stderr is not None else self.stderr,
            )
        executable = self.resolve_command(argv[0], env)
        if executable is None:
            (stderr or self.stderr).write(f"{argv[0]}: command not found\n")
            return 127
        env = prepare_external_environment(executable, argv, env)

        stdin = stdin if stdin is not None else self.stdin
        stdout = stdout if stdout is not None else self.stdout
        stderr = stderr if stderr is not None else self.stderr

        popen_stdin: object = stdin
        popen_stdout: object = stdout
        popen_stderr: object = stderr
        communicate_input = input_text
        binary_process = isinstance(input_text, bytes) or capture_stdout

        if input_text is not None:
            if binary_process and isinstance(communicate_input, str):
                communicate_input = communicate_input.encode("utf-8")
            popen_stdin = subprocess.PIPE
        elif not has_fileno(stdin):
            communicate_input = stdin.read()
            if binary_process:
                communicate_input = communicate_input.encode("utf-8")
            popen_stdin = subprocess.PIPE

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
            command_argv = subprocess_argv_for_windows_script(executable, argv[1:]) if is_windows_batch_file(executable) else [executable, *argv[1:]]
            process = subprocess.Popen(
                command_argv,
                stdin=popen_stdin,
                stdout=popen_stdout,
                stderr=popen_stderr,
                env=env,
                text=not binary_process,
            )
            completed_stdout, completed_stderr = process.communicate(input=communicate_input)
        except PermissionError:
            (stderr or self.stderr).write(f"{argv[0]}: permission denied\n")
            return 126
        except FileNotFoundError:
            (stderr or self.stderr).write(f"{argv[0]}: command not found\n")
            return 127
        except KeyboardInterrupt:
            if "process" in locals():
                stop_process_after_interrupt(process)
            self._last_external_output = ""
            return 130
        except OSError as exc:
            (stderr or self.stderr).write(f"{argv[0]}: {exc}\n")
            return 126

        if capture_stdout and completed_stdout:
            self._last_external_output = completed_stdout
        else:
            self._last_external_output = ""
        if stdout_capture_target is not None and completed_stdout:
            write_pipeline_process_output(stdout_capture_target, completed_stdout)
        if stderr_capture_target is not None and completed_stderr:
            write_pipeline_process_output(stderr_capture_target, completed_stderr)
        return normalize_process_status(process.returncode)

    def run_preexpanded(self, argv: list[str], *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
        if not argv:
            return 0
        if argv[0] in BUILTINS and argv[0] != "command":
            return BUILTINS[argv[0]](self, argv, stdin, stdout, stderr)
        return self.run_external(argv, stdin=stdin, stdout=stdout, stderr=stderr)

    @contextlib.contextmanager
    def using_streams(self, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> Iterator[None]:
        old_stdin, old_stdout, old_stderr = self.stdin, self.stdout, self.stderr
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        try:
            yield
        finally:
            self.stdin, self.stdout, self.stderr = old_stdin, old_stdout, old_stderr

    @contextlib.contextmanager
    def suppress_errexit(self) -> Iterator[None]:
        self._suppress_errexit += 1
        try:
            yield
        finally:
            self._suppress_errexit -= 1

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
                target = normalize_redirection_target(target)
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
                elif op == "<>":
                    file = open(target, "r+", encoding="utf-8", errors="replace")
                    opened.append(file)
                    streams[fd] = file
                elif op in {"<<", "<<-"}:
                    streams[fd] = io.StringIO(target)
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
            prompt_source = self.get_parameter("PS2") if buffer else self.get_parameter("PS1") or "$ "
            try:
                prompt = self.expand_prompt(prompt_source)
            except KeyboardInterrupt:
                self.stdout.write("\n")
                buffer = ""
                status = 130
                self.last_status = status
                continue
            except ExpansionError as exc:
                self.stderr.write(f"{self.argv0}: {exc}\n")
                prompt = prompt_source
                status = 2
                self.last_status = status
            try:
                line = self.read_input_line(prompt)
            except EOFError:
                self.stdout.write("\n")
                return status
            except KeyboardInterrupt:
                self.stdout.write("\n")
                buffer = ""
                status = 130
                self.last_status = status
                continue
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
                self.last_status = status
                continue
            except ParseError as exc:
                self.stderr.write(f"{self.argv0}: syntax error: {exc}\n")
                buffer = ""
                status = 2
                self.last_status = status
                continue
            except KeyboardInterrupt:
                self.stdout.write("\n")
                buffer = ""
                status = 130
                self.last_status = status
                continue
            self.add_history(candidate)
            try:
                status = self.execute(candidate)
            except ShellExit as exc:
                return exc.status
            except KeyboardInterrupt:
                self.stdout.write("\n")
                status = 130
                self.last_status = status
            buffer = ""

    def read_input_line(self, prompt: str) -> str:
        if self.stdin is sys.stdin and self.stdout is sys.stdout and sys.stdin.isatty():
            return self.read_console_line(prompt)
        return self.process_tabs_in_input(input(prompt), prompt)

    def process_tabs_in_input(self, line: str, prompt: str) -> str:
        if "\t" not in line:
            return line
        buffer = ""
        for char in line:
            if char == "\t":
                result = self.complete_line_for_tab(buffer)
                old_buffer = buffer
                buffer = result.line
                self.render_completion_result(result, prompt, old_buffer)
            else:
                buffer += char
        return buffer

    def read_console_line(self, prompt: str) -> str:
        self.stdout.write(prompt)
        self.stdout.flush()
        if os.name == "nt":
            return self.read_windows_console_line(prompt)
        return self.read_posix_console_line(prompt)

    def read_windows_console_line(self, prompt: str) -> str:
        import msvcrt

        line = ""
        history_state = LineHistoryState(index=len(self.history))
        while True:
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                code = msvcrt.getwch()
                if code == "H":
                    line = self.apply_history_navigation(prompt, line, history_state, -1)
                elif code == "P":
                    line = self.apply_history_navigation(prompt, line, history_state, 1)
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char == "\x04":
                raise EOFError
            if char in {"\r", "\n"}:
                self.stdout.write("\n")
                return line
            if char == "\t":
                result = self.complete_line_for_tab(line)
                self.render_completion_result(result, prompt, line)
                line = result.line
                continue
            if char in {"\b", "\x7f"}:
                if line:
                    line = self.erase_input_character(line)
                continue
            if char >= " ":
                line += char
                self.stdout.write(char)
                self.stdout.flush()

    def read_posix_console_line(self, prompt: str) -> str:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        line = ""
        history_state = LineHistoryState(index=len(self.history))
        try:
            tty.setraw(fd)
            while True:
                char = sys.stdin.read(1)
                if char == "\x03":
                    raise KeyboardInterrupt
                if char == "\x04":
                    raise EOFError
                if char in {"\r", "\n"}:
                    self.stdout.write("\n")
                    return line
                if char == "\t":
                    result = self.complete_line_for_tab(line)
                    self.render_completion_result(result, prompt, line)
                    line = result.line
                    continue
                if char in {"\b", "\x7f"}:
                    if line:
                        line = self.erase_input_character(line)
                    continue
                if char == "\x1b":
                    key = self.read_posix_escape_sequence()
                    if key == "UP":
                        line = self.apply_history_navigation(prompt, line, history_state, -1)
                    elif key == "DOWN":
                        line = self.apply_history_navigation(prompt, line, history_state, 1)
                    continue
                if char >= " ":
                    line += char
                    self.stdout.write(char)
                    self.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    def read_posix_escape_sequence(self) -> str | None:
        if sys.stdin.read(1) != "[":
            return None
        code = sys.stdin.read(1)
        if code == "A":
            return "UP"
        if code == "B":
            return "DOWN"
        return None

    def apply_history_navigation(
        self, prompt: str, line: str, state: LineHistoryState, direction: int
    ) -> str:
        old_line = line
        line, moved = self.navigate_input_history(line, state, direction)
        if moved:
            self.redraw_input_line(prompt, old_line, line)
        else:
            self.stdout.write("\a")
            self.stdout.flush()
        return line

    def navigate_input_history(
        self, line: str, state: LineHistoryState, direction: int
    ) -> tuple[str, bool]:
        if not self.history:
            return line, False
        history_end = len(self.history)
        if state.index == history_end:
            state.saved_line = line
        if direction < 0:
            if state.index == 0:
                return line, False
            state.index -= 1
            return self.history_entry_for_input(self.history[state.index]), True
        if state.index >= history_end:
            return line, False
        state.index += 1
        if state.index == history_end:
            return state.saved_line, True
        return self.history_entry_for_input(self.history[state.index]), True

    def history_entry_for_input(self, entry: str) -> str:
        return " ".join(entry.splitlines())

    def redraw_input_line(self, prompt: str, old_line: str, new_line: str) -> None:
        clear_width = terminal_display_width(prompt) + max(
            terminal_display_width(old_line),
            terminal_display_width(new_line),
        )
        self.stdout.write("\r" + (" " * clear_width) + "\r" + prompt + new_line)
        self.stdout.flush()

    def erase_input_character(self, line: str) -> str:
        char = line[-1]
        width = max(1, terminal_display_width(char))
        self.stdout.write(("\b" * width) + (" " * width) + ("\b" * width))
        self.stdout.flush()
        return line[:-1]

    def complete_line_for_tab(self, line: str) -> CompletionResult:
        if not line or line[-1].isspace():
            return CompletionResult(line=line, beep=True)
        start = len(line)
        while start > 0 and not line[start - 1].isspace():
            start -= 1
        prefix = line[start:]
        try:
            matches = sorted(entry.name for entry in Path(".").iterdir() if entry.name.startswith(prefix))
        except OSError:
            return CompletionResult(line=line, beep=True)
        if not matches:
            return CompletionResult(line=line, beep=True)
        if len(matches) == 1:
            return CompletionResult(line=line[:start] + matches[0])
        common = os.path.commonprefix(matches)
        if len(common) > len(prefix):
            return CompletionResult(line=line[:start] + common)
        shown = tuple(matches[:10])
        return CompletionResult(line=line, beep=True, listings=shown, hidden_count=len(matches) - len(shown))

    def render_completion_result(self, result: CompletionResult, prompt: str, previous_line: str) -> None:
        if result.beep:
            self.stdout.write("\a")
        if result.listings:
            self.stdout.write("\n")
            for item in result.listings:
                self.stdout.write(item + "\n")
            if result.hidden_count > 0:
                self.stdout.write(f"... {result.hidden_count} terms hidden ...\n")
            self.stdout.write(prompt + result.line)
        elif result.line.startswith(previous_line) and len(result.line) > len(previous_line):
            self.stdout.write(result.line[len(previous_line) :])
        self.stdout.flush()

    def expand_prompt(self, prompt: str) -> str:
        return chars_to_text(expand_text(self, prompt, quoted=True))

    def add_history(self, source: str) -> None:
        entry = source.rstrip("\n")
        if entry:
            self.history.append(entry)

    def get_parameter(self, name: str, *, strict: bool = False) -> str:
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
        if name in self.env:
            return self.env[name]
        if strict and self.options.get("nounset", False):
            raise ExpansionError(f"{name}: parameter not set")
        return ""

    def parameter_is_set(self, name: str) -> bool:
        if name in {"?", "$", "#", "0", "@", "*"}:
            return True
        if len(name) == 1 and name.isdigit():
            return 0 <= int(name) - 1 < len(self.positional)
        return name in self.vars or name in self.env

    def set_parameter(self, name: str, value: str, *, export: bool = False) -> None:
        if name in self.readonly and self.get_parameter(name) != value:
            raise ExpansionError(f"{name}: is read only")
        self.vars[name] = value
        if export or name in self.env:
            self.env[name] = value

    def unset_parameter(self, name: str) -> None:
        if name in self.readonly:
            raise ExpansionError(f"{name}: is read only")
        self.vars.pop(name, None)
        self.env.pop(name, None)

    def is_builtin(self, name: str) -> bool:
        return name in BUILTINS

    def is_internal_utility(self, name: str) -> bool:
        return name in INTERNAL_UTILITIES

    def which(self, name: str) -> str | None:
        if self.should_run_internal_utility(name, self.env):
            return name
        path = self.resolve_command(name, self.env)
        if path:
            return path
        if name in INTERNAL_UTILITIES:
            return name
        return None

    def resolve_command(self, name: str, env: dict[str, str]) -> str | None:
        if any(sep in name for sep in ("/", "\\")):
            native = normalize_path_entry(name)
            if os.path.exists(native):
                return os.path.abspath(native)
            for candidate in executable_path_candidates(name, env):
                if is_executable_file(candidate):
                    return os.path.abspath(candidate)
            return None
        if name in self.command_hash:
            cached = normalize_path_entry(self.command_hash[name])
            if os.path.exists(cached):
                return cached
        matches = find_executable_matches(name, env)
        if matches:
            return matches[0]
        if os.name == "nt" and name == "vi":
            vim_matches = find_executable_matches("vim", env)
            if vim_matches:
                return vim_matches[0]
            return shutil.which("vim", path=env.get("PATH"))
        return None

    def should_run_internal_utility(self, name: str, env: dict[str, str]) -> bool:
        if name not in INTERNAL_UTILITIES:
            return False
        if name == "help":
            return True
        if name == "vi" and os.name != "nt":
            return False
        if name == "ps" and os.name != "nt":
            return False
        if name == "find" and os.name == "nt":
            return True
        if name == "which":
            return os.name == "nt"
        return self.resolve_command(name, env) is None

    def run_exit_trap(self) -> None:
        action = self.traps.get("EXIT") or self.traps.get("0")
        if not action or self._running_exit_trap:
            return
        saved_status = self.last_status
        self._running_exit_trap = True
        try:
            self.execute(action, allow_return=True)
        except ShellExit:
            pass
        finally:
            self.last_status = saved_status
            self._running_exit_trap = False


def has_fileno(stream: TextIO) -> bool:
    try:
        stream.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return False
    return True


def resolve_shell_environment_value(candidate: str, env: dict[str, str]) -> str:
    candidate = candidate or "pysh"
    native = normalize_path_entry(candidate)
    if os.path.isabs(native) or any(sep in candidate for sep in ("/", "\\")):
        return os.path.abspath(native)
    matches = find_executable_matches(candidate, env)
    if matches:
        return matches[0]
    found = shutil.which(candidate, path=env.get("PATH"))
    if found:
        return os.path.abspath(found)
    return candidate


def ensure_cygpath_console_script_on_path(env: dict[str, str]) -> None:
    if os.name != "nt":
        return
    shell_path = env.get("SHELL", "")
    if not shell_path or not any(sep in shell_path for sep in ("/", "\\")):
        return
    shell_dir = os.path.dirname(os.path.abspath(normalize_path_entry(shell_path)))
    if not shell_dir:
        return
    if not os.path.exists(os.path.join(shell_dir, "cygpath.exe")):
        return
    existing = {
        os.path.normcase(os.path.abspath(normalize_path_entry(entry or ".")))
        for entry in split_path_list(env.get("PATH", ""))
    }
    shell_dir_key = os.path.normcase(os.path.abspath(shell_dir))
    if shell_dir_key in existing:
        return
    path_text = env.get("PATH", "")
    env["PATH"] = f"{path_text}{os.pathsep if path_text else ''}{shell_dir}"


def prepare_external_environment(executable: str, argv: list[str], env: dict[str, str]) -> dict[str, str]:
    child_env = dict(env)
    if is_conda_posix_shell_command(executable, argv):
        ensure_cygpath_console_script_on_path(child_env)
        child_env["PATH"] = remove_bash_without_cygpath_from_path(child_env.get("PATH", ""))
    return child_env


def is_conda_posix_shell_command(executable: str, argv: list[str]) -> bool:
    if os.name != "nt" or len(argv) < 2:
        return False
    name = os.path.basename(executable).lower()
    if name not in {"conda", "conda.exe", "conda.bat", "conda.cmd"}:
        return False
    return argv[1].startswith("shell.posix")


def remove_bash_without_cygpath_from_path(path_text: str) -> str:
    if os.name != "nt" or not path_text:
        return path_text
    kept: list[str] = []
    for entry in split_path_list(path_text):
        native = normalize_path_entry(entry or ".")
        if os.path.exists(os.path.join(native, "bash.exe")) and not os.path.exists(os.path.join(native, "cygpath.exe")):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def pipeline_input_stream(value: PipelineValue) -> TextIO:
    if isinstance(value, bytes):
        return io.TextIOWrapper(io.BytesIO(value), encoding="utf-8", errors="replace", newline="")
    return io.StringIO(value)


def write_pipeline_process_output(stream: TextIO, output: str | bytes) -> None:
    if isinstance(output, str):
        stream.write(output)
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(output)
        buffer.flush()
    else:
        stream.write(output.decode("utf-8", errors="replace"))


def is_windows_batch_file(path: str) -> bool:
    return os.name == "nt" and os.path.splitext(path)[1].lower() in {".bat", ".cmd"}


def subprocess_argv_for_windows_script(executable: str, args: list[str]) -> list[str]:
    command = " ".join(cmd_quote(arg) for arg in [executable, *args])
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]


def cmd_quote(value: str) -> str:
    if value == "":
        return '""'
    if not any(char.isspace() or char in '"&()[]{}^=;!%+`,~|' for char in value):
        return value
    return '"' + value.replace('"', r'\"') + '"'


def terminal_display_width(text: str) -> int:
    width = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\033":
            index += 1
            if index < len(text) and text[index] == "[":
                index += 1
                while index < len(text) and not ("@" <= text[index] <= "~"):
                    index += 1
                index += 1
                continue
            continue
        if unicodedata.combining(char):
            index += 1
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        index += 1
    return width


def normalize_redirection_target(target: str) -> str:
    if os.name == "nt" and target == "/dev/null":
        return os.devnull
    return target


def stop_process_after_interrupt(process: subprocess.Popen[str]) -> None:
    try:
        process.wait(timeout=0.2)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return

    try:
        if os.name == "nt":
            process.terminate()
        else:
            process.send_signal(signal.SIGINT)
    except OSError:
        return

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            return
    except OSError:
        return


def normalize_process_status(returncode: int) -> int:
    if os.name == "nt" and returncode in {0xC000013A, -1073741510}:
        return 130
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def invert_status(status: int) -> int:
    return 0 if status != 0 else 1


def quote_shell_arg(value: str) -> str:
    return shlex.quote(value)


def prepare_heredocs(source: str) -> tuple[str, dict[str, list[HereDoc]]]:
    lines = source.splitlines(keepends=True)
    rewritten: list[str] = []
    here_docs: dict[str, list[HereDoc]] = {}
    index = 0

    while index < len(lines):
        line = lines[index]
        rewritten.append(line)
        pending = find_here_doc_specs(line)
        index += 1

        for delimiter, strip_tabs, should_expand in pending:
            body: list[str] = []
            while index < len(lines):
                raw = lines[index]
                check = raw.rstrip("\r\n")
                if strip_tabs:
                    check = check.lstrip("\t")
                if check == delimiter:
                    index += 1
                    break
                body.append(raw.lstrip("\t") if strip_tabs else raw)
                index += 1
            else:
                raise ParseError(f"here-document delimited by end-of-file, wanted {delimiter!r}")
            here_docs.setdefault(delimiter, []).append(HereDoc("".join(body), should_expand))

    return "".join(rewritten), here_docs


def find_here_doc_specs(line: str) -> list[tuple[str, bool, bool]]:
    try:
        tokens = lex(line)
    except LexerError:
        return []

    specs: list[tuple[str, bool, bool]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, Operator) and token.value in {"<<", "<<-"}:
            if index + 1 >= len(tokens) or not isinstance(tokens[index + 1], Word):
                raise ParseError(f"expected here-document delimiter after {token.value}")
            delimiter = tokens[index + 1]
            specs.append((delimiter.text, token.value == "<<-", not delimiter.has_quoted_part))
            index += 2
            continue
        index += 1
    return specs
