# py-posix-shell

`py-posix-shell` is a small POSIX-style shell implemented in pure Python. It is
intended to run on Windows, Linux, and macOS with the same Python package and the
same console entry point.

The project focuses on a practical, compact subset of POSIX shell behavior:

- command lists with `;`, `&&`, `||`, and background `&`
- pipelines with `|`
- quoting with single quotes, double quotes, and backslash escapes
- parameter expansion such as `$HOME`, `$?`, `${name:-word}`, and `${name:=word}`
- command substitution with `$(...)` and backticks
- field splitting, pathname expansion, and tilde expansion
- redirection with `<`, `>`, `>>`, `2>`, `2>>`, `>&`, and `<&`
- shell variables, exported environment variables, and positional parameters
- common builtins including `cd`, `pwd`, `exit`, `export`, `unset`, `set`,
  `shift`, `echo`, `printf`, `read`, `type`, `command`, `env`, `test`, and `[`

It deliberately does not try to be a full replacement for `dash`, `bash`, or
`zsh`. Complex POSIX features such as functions, aliases, job control, traps,
arithmetic expansion, case/esac, for/while/until loops, and here-documents are
not implemented yet.

## Installation

From this repository:

```bash
pip install .
```

For editable development:

```bash
pip install -e ".[dev]"
```

## Usage

Start an interactive shell:

```bash
pysh
```

Run one command:

```bash
pysh -c "name=world; echo \"hello $name\""
```

Run a script:

```bash
pysh ./script.sh arg1 arg2
```

The package also installs the longer `py-posix-shell` command.

## Development

Run the tests with:

```bash
pytest
```

The implementation uses only the Python standard library at runtime.
