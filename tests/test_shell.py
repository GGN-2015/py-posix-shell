from __future__ import annotations

import io
import os
import sys

import py_posix_shell.shell as shell_module
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


def test_internal_posix_utilities_work_without_path(tmp_path):
    env = {"PATH": ""}
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    moved = tmp_path / "moved.txt"
    nested = tmp_path / "a" / "b"
    src.write_text("alpha\nbeta\nalpha-two\n", encoding="utf-8")
    script = f'''
mkdir -p "{nested}"
cp "{src}" "{dst}"
mv "{dst}" "{moved}"
printf "extra\\n" >> "{moved}"
ls -1 "{tmp_path}"
cat "{moved}" | grep alpha | wc -l
head -n 1 "{moved}"
tail -n 1 "{moved}"
touch "{tmp_path / 'new.txt'}"
rm "{tmp_path / 'new.txt'}"
rmdir "{nested}"
rmdir "{nested.parent}"
basename "{moved}"
dirname "{moved}"
'''
    status, stdout, stderr, _shell = run_shell(script, env=env)
    assert status == 0
    assert "moved.txt" in stdout
    assert "\n2\n" in stdout
    assert "alpha\n" in stdout
    assert "extra\n" in stdout
    assert "moved.txt\n" in stdout
    assert str(tmp_path) in stdout
    assert not (tmp_path / "new.txt").exists()
    assert not nested.parent.exists()
    assert stderr == ""


def test_internal_process_and_identity_utilities_without_path():
    status, stdout, stderr, _shell = run_shell(
        "ps; uname -s; whoami; id; date +%Y",
        env={"PATH": ""},
    )
    assert status == 0
    assert "PID COMMAND\n" in stdout
    assert stdout.count("\n") >= 5
    assert stderr == ""


def test_command_and_type_report_internal_utility_when_path_is_empty():
    status, stdout, stderr, _shell = run_shell("command -v ls; type ls", env={"PATH": ""})
    assert status == 0
    assert stdout.splitlines()[0] == "ls"
    assert "ls is a shell utility" in stdout
    assert stderr == ""


def test_external_keyboard_interrupt_returns_130(monkeypatch):
    shell = Shell(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
    shell.resolve_command = lambda _name, _env: "fake-more"  # type: ignore[method-assign]
    events: list[str] = []

    class FakeProcess:
        returncode = None

        def communicate(self, input=None):
            events.append(f"communicate:{input!r}")
            raise KeyboardInterrupt

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            if timeout == 0.2:
                raise shell_module.subprocess.TimeoutExpired("fake-more", timeout)
            self.returncode = -2
            return self.returncode

        def terminate(self):
            events.append("terminate")

        def send_signal(self, sig):
            events.append(f"signal:{sig}")

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(shell_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    assert shell.run_external(["more"]) == 130
    assert "communicate:''" in events
    assert "terminate" in events or any(event.startswith("signal:") for event in events)
    assert shell.stderr.getvalue() == ""


def test_repl_keyboard_interrupt_returns_to_prompt(monkeypatch):
    stdout = io.StringIO()
    shell = Shell(stdout=stdout, stderr=io.StringIO(), interactive=True)
    events = iter([KeyboardInterrupt, EOFError])

    def fake_input(_prompt):
        raise next(events)

    monkeypatch.setattr("builtins.input", fake_input)

    assert shell.repl() == 130
    assert shell.last_status == 130
    assert stdout.getvalue() == "\n\n"


def test_internal_extended_text_utilities_without_path(tmp_path):
    data = tmp_path / "data.txt"
    csv = tmp_path / "data.csv"
    data.write_text("beta\nalpha\nalpha\ngamma\n", encoding="utf-8")
    csv.write_text("1,red\n2,blue\n", encoding="utf-8")
    script = f'''
sort "{data}" | uniq
sort -r "{data}" | head -n 1
cut -d, -f2 "{csv}"
grep -c alpha "{data}"
egrep "alpha|gamma" "{data}" | wc -l
fgrep "a.b" "{data}"
echo fixed-miss:$?
sed -n "s/alpha/ALPHA/p" "{data}"
gawk -F, '{{ print $2 }}' "{csv}"
yes ok | head -n 2
'''
    status, stdout, stderr, _shell = run_shell(script, env={"PATH": ""})
    assert status == 0
    assert "alpha\nbeta\ngamma\n" in stdout
    assert "gamma\n" in stdout
    assert "red\nblue\n" in stdout
    assert "\n2\n" in stdout
    assert "fixed-miss:1\n" in stdout
    assert "ALPHA\nALPHA\n" in stdout
    assert stdout.endswith("ok\nok\n")
    assert stderr == ""


def test_internal_findutils_and_file_install_without_path(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("payload\n", encoding="utf-8")
    installed = tmp_path / "dest" / "bin" / "source.txt"
    database = tmp_path / "locatedb"
    script = f'''
install -D -m 755 "{source}" "{installed}"
cat "{installed}"
find "{tmp_path}" -name "*.txt" | sort
printf "one two\\n" | xargs -n1 echo item
updatedb -o "{database}" -U "{tmp_path}"
locate -d "{database}" source.txt
unlink "{installed}"
'''
    status, stdout, stderr, _shell = run_shell(script, env={"PATH": ""})
    assert status == 0
    assert "payload\n" in stdout
    assert str(source) in stdout
    assert str(installed) in stdout
    assert "item one\nitem two\n" in stdout
    assert not installed.exists()
    assert stderr == ""


def test_internal_diffutils_and_tar_without_path(tmp_path):
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    base = tmp_path / "base.txt"
    tar_src = tmp_path / "tar-src"
    tar_out = tmp_path / "tar-out"
    archive = tmp_path / "archive.tar"
    left.write_text("one\ntwo\n", encoding="utf-8")
    right.write_text("one\nthree\n", encoding="utf-8")
    base.write_text("one\n", encoding="utf-8")
    tar_src.mkdir()
    (tar_src / "entry.txt").write_text("inside\n", encoding="utf-8")
    tar_out.mkdir()
    script = f'''
diff -u "{left}" "{right}"
echo diff:$?
cmp -s "{left}" "{left}"
cmp -s "{left}" "{right}"
echo cmp:$?
diff3 "{left}" "{base}" "{right}"
echo diff3:$?
sdiff "{left}" "{right}"
echo sdiff:$?
tar -cf "{archive}" -C "{tar_src}" entry.txt
tar -tf "{archive}"
tar -xf "{archive}" -C "{tar_out}"
cat "{tar_out / 'entry.txt'}"
'''
    status, stdout, stderr, _shell = run_shell(script, env={"PATH": ""})
    assert status == 0
    assert "--- " in stdout and "+++ " in stdout
    assert "diff:1\n" in stdout
    assert "cmp:1\n" in stdout
    assert "diff3:1\n" in stdout
    assert "sdiff:1\n" in stdout
    assert "entry.txt\ninside\n" in stdout
    assert stderr == ""
