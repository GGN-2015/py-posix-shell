# py-posix-shell

`py-posix-shell` is a cross-platform POSIX-style shell implemented in pure
Python. The long-term goal is to become a practical `dash`-like shell that can
run on Windows, Linux, and macOS from the same Python package.

It is still young, but it now supports the pieces needed by many ordinary shell
scripts:

- command lists with `;`, `&&`, `||`, background `&`, and `!`
- pipelines with `|`
- `if`/`elif`/`else`/`fi`
- `for`, `while`, and `until` loops with `break` and `continue`
- `case`/`esac`
- POSIX-style functions with `return`
- grouping with `{ ...; }` and subshells with `( ... )`
- quoting with single quotes, double quotes, and backslash escapes
- parameter expansion such as `$HOME`, `$?`, `${name:-word}`, `${name:=word}`,
  `${#name}`, `${name#pattern}`, `${name##pattern}`, `${name%pattern}`, and
  `${name%%pattern}`
- arithmetic expansion with `$(( expression ))`
- command substitution with `$(...)` and backticks
- field splitting, pathname expansion, tilde expansion, and `set -f`
- here-documents with `<<` and `<<-`
- redirection with `<`, `>`, `>>`, `<>`, `2>`, `2>>`, `>&`, and `<&`
- shell variables, exported environment variables, and positional parameters
- common builtins including `.`, `:`, `alias`, `unalias`, `cd`, `pwd`, `eval`,
  `exec`, `exit`, `export`, `readonly`, `unset`, `set`, `shift`, `getopts`,
  `echo`, `printf`, `read`, `type`, `command`, `env`, `test`, `[`, `trap`,
  `umask`, `times`, and `hash`

The implementation intentionally remains dependency-free at runtime. It aims for
useful POSIX behavior first, then progressively tighter compatibility with
`dash`.

Known gaps include job control, full signal semantics, complete interactive line
editing, and exact `set -e` edge cases.

## Installation

From PyPI:

```bash
pip install py-posix-shell
```

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

Build the package with:

```bash
poetry build
```
