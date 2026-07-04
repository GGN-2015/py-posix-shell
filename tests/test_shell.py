from __future__ import annotations

import io
import os
import sys

from py_posix_shell.lexer import dump_tokens, lex
from py_posix_shell.errors import ShellExit
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


def test_if_for_while_and_arithmetic():
    script = """
if false; then
  echo bad
elif true; then
  echo good
else
  echo bad
fi

for item in a b; do
  echo "for:$item"
done

i=0
while [ "$i" -lt 3 ]; do
  i=$((i + 1))
  [ "$i" -eq 2 ] && continue
  echo "while:$i"
done
"""
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 0
    assert stdout == "good\nfor:a\nfor:b\nwhile:1\nwhile:3\n"
    assert stderr == ""


def test_until_case_function_and_return():
    script = """
say() {
  case "$1" in
    a|b) echo "letter:$1" ;;
    *) echo other ;;
  esac
  return 7
}
say b
echo "status:$?"

i=0
until [ "$i" -eq 2 ]; do
  i=$((i + 1))
done
echo "until:$i"
"""
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 0
    assert stdout == "letter:b\nstatus:7\nuntil:2\n"
    assert stderr == ""


def test_here_documents_and_parameter_operators():
    script = r"""
name=world
while read line; do echo "$line"; done <<EOF
hello $name
EOF
while read line; do echo "$line"; done <<'EOF'
literal $name
EOF
path=/usr/local/bin
echo "${#name}:${path##*/}:${path%/*}"
"""
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 0
    assert stdout == "hello world\nliteral $name\n5:bin:/usr/local\n"
    assert stderr == ""


def test_dot_eval_and_quoted_at(tmp_path):
    script_file = tmp_path / "lib.sh"
    script_file.write_text("from_dot=ok\nreturn 4\n", encoding="utf-8")
    script = f'''
. "{script_file}"
echo "$from_dot:$?"
set -- "a b" c
for arg in "$@"; do echo "arg:$arg"; done
eval 'echo eval:$from_dot'
'''
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 0
    assert stdout == "ok:4\narg:a b\narg:c\neval:ok\n"
    assert stderr == ""


def test_group_subshell_negation_and_set_options(tmp_path):
    (tmp_path / "match.txt").write_text("", encoding="utf-8")
    old = os.getcwd()
    try:
        os.chdir(tmp_path)
        script = """
{ echo grouped; } > grouped.out
( x=changed; echo "sub:$x" )
echo "parent:${x:-empty}"
! false
echo "not:$?"
set -f
echo *.txt
set +f
echo *.txt
set -u
echo "$missing"
"""
        status, stdout, stderr, _shell = run_shell(script)
        assert status == 2
        assert stdout == "sub:changed\nparent:empty\nnot:0\n*.txt\nmatch.txt\n"
        assert "missing: parameter not set" in stderr
        assert (tmp_path / "grouped.out").read_text(encoding="utf-8") == "grouped\n"
    finally:
        os.chdir(old)


def test_set_e_exits_on_uncontrolled_failure_but_not_and_or():
    stdout = io.StringIO()
    stderr = io.StringIO()
    shell = Shell(stdout=stdout, stderr=stderr)
    try:
        shell.execute("set -e; false && echo skipped; echo alive; false; echo dead")
    except ShellExit as exc:
        assert exc.status == 1
    else:
        raise AssertionError("set -e did not exit")
    assert stdout.getvalue() == "alive\n"
    assert stderr.getvalue() == ""


def test_alias_unalias_and_arithmetic_side_effects():
    script = """
alias hi='echo hello'
hi world
unalias hi
i=1
echo "$((i++)):$i"
echo "$((++i)):$i"
echo "$((i += 5)):$i"
"""
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 0
    assert stdout == "hello world\n1:2\n3:3\n8:8\n"
    assert stderr == ""


def test_trap_exit_runs_with_original_status():
    stdout = io.StringIO()
    stderr = io.StringIO()
    shell = Shell(stdout=stdout, stderr=stderr)
    try:
        shell.execute("trap 'echo exit:$?' EXIT; echo body; exit 7")
    except ShellExit as exc:
        assert exc.status == 7
    else:
        raise AssertionError("exit did not raise")
    assert stdout.getvalue() == "body\nexit:7\n"
    assert stderr.getvalue() == ""


def test_getopts_hash_umask_times_and_more_test_ops(tmp_path):
    data = tmp_path / "data.txt"
    link = tmp_path / "data-link.txt"
    devnull = os.devnull
    data.write_text("x", encoding="utf-8")
    try:
        link.symlink_to(data)
    except OSError:
        link = data
    script = f'''
set -- -a -b value rest
while getopts ab: opt; do
  echo "opt:$opt:$OPTARG:$OPTIND"
done
[ -s "{data}" ] && echo sized
[ "{data}" -ef "{link}" ] && echo same
[ "{data}" -nt "{tmp_path / 'missing'}" ] || echo nt-missing
hash "{sys.executable}" || true
umask > "{devnull}"
times > "{devnull}"
'''
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 0
    assert "opt:a::2\n" in stdout
    assert "opt:b:value:4\n" in stdout
    assert "sized\n" in stdout
    assert "same\n" in stdout
    assert "nt-missing\n" in stdout
    assert stderr == ""


def test_read_write_redirection_and_readonly(tmp_path):
    path = tmp_path / "rw.txt"
    path.write_text("abc", encoding="utf-8")
    script = f'''
exec 3<> "{path}"
readonly locked=one
locked=two
'''
    status, stdout, stderr, _shell = run_shell(script)
    assert status == 2
    assert stdout == ""
    assert "locked: is read only" in stderr
