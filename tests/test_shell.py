from __future__ import annotations

import io
import os
import sys

from py_posix_shell.lexer import dump_tokens, lex
from py_posix_shell.shell import Shell


def run_shell(source: str, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    shell = Shell(stdout=stdout, stderr=stderr, **kwargs)
    status = shell.execute(source)
    return status, stdout.getvalue(), stderr.getvalue(), shell


def test_lexer_keeps_quotes_and_operators():
    assert dump_tokens(lex("echo 'a b' \"$HOME\" && false")) == [
        "word:echo",
        "word:a b",
        "word:$HOME",
        "op:&&",
        "word:false",
    ]


def test_assignment_and_parameter_expansion():
    status, stdout, stderr, _shell = run_shell("name=world; echo hello-$name")
    assert status == 0
    assert stdout == "hello-world\n"
    assert stderr == ""


def test_and_or_status():
    status, stdout, _stderr, _shell = run_shell("false && echo no; false || echo yes")
    assert status == 0
    assert stdout == "yes\n"


def test_redirection(tmp_path):
    path = tmp_path / "out.txt"
    status, stdout, stderr, _shell = run_shell(f'echo saved > "{path}"')
    assert status == 0
    assert stdout == ""
    assert stderr == ""
    assert path.read_text(encoding="utf-8") == "saved\n"


def test_pipeline_with_builtin_and_external_python():
    code = "import sys; print(sys.stdin.read().upper(), end='')"
    status, stdout, stderr, _shell = run_shell(f'echo hello | "{sys.executable}" -c "{code}"')
    assert status == 0
    assert stdout == "HELLO\n"
    assert stderr == ""


def test_command_substitution():
    status, stdout, stderr, _shell = run_shell("echo $(printf hi)")
    assert status == 0
    assert stdout == "hi\n"
    assert stderr == ""


def test_parameter_defaults_and_escaped_dollar():
    status, stdout, stderr, _shell = run_shell(r'echo ${missing:-fallback}; name=${name:=set}; echo "\$name" "$name"')
    assert status == 0
    assert stdout == "fallback\n$name set\n"
    assert stderr == ""


def test_cd_updates_pwd(tmp_path):
    old = os.getcwd()
    try:
        status, stdout, stderr, shell = run_shell(f'cd "{tmp_path}"; pwd')
        assert status == 0
        assert stdout == str(tmp_path) + "\n"
        assert stderr == ""
        assert shell.get_parameter("PWD") == str(tmp_path)
    finally:
        os.chdir(old)
