"""Shell word expansion."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

from .errors import ExpansionError
from .lexer import DOUBLE, PLAIN, SINGLE, Part, Word, is_name


@dataclass(frozen=True)
class Char:
    value: str
    quoted: bool = False


def expand_word(shell, word: Word, *, field_split: bool = True, pathname: bool = True) -> list[str]:
    chars, had_quoted = expand_word_to_chars(shell, word)
    if field_split:
        fields = split_fields(shell, chars, had_quoted)
    elif chars or had_quoted:
        fields = [chars]
    else:
        fields = []

    result: list[str] = []
    for field in fields:
        text = chars_to_text(field)
        if pathname and has_unquoted_glob(field):
            matches = sorted(glob.glob(text))
            if matches:
                result.extend(matches)
                continue
        result.append(text)
    return result


def expand_assignment(shell, word: Word) -> str:
    chars, _had_quoted = expand_word_to_chars(shell, expand_assignment_tilde(word))
    return chars_to_text(chars)


def expand_redirection(shell, word: Word) -> str:
    fields = expand_word(shell, word, field_split=False, pathname=False)
    if len(fields) != 1:
        raise ExpansionError("ambiguous redirect")
    return fields[0]


def expand_word_to_chars(shell, word: Word) -> tuple[list[Char], bool]:
    word = expand_initial_tilde(word)
    chars: list[Char] = []
    had_quoted = word.has_quoted_part
    for part in word.parts:
        if part.mode == SINGLE:
            chars.extend(Char(char, True) for char in part.text)
        elif part.mode == DOUBLE:
            chars.extend(expand_text(shell, part.text, quoted=True))
        else:
            chars.extend(expand_text(shell, part.text, quoted=False))
    return chars, had_quoted


def expand_initial_tilde(word: Word) -> Word:
    if not word.parts:
        return word
    first = word.parts[0]
    if first.mode != PLAIN or not first.text.startswith("~"):
        return word
    match = re.match(r"~[^/\\]*", first.text)
    if not match:
        return word
    user_expr = match.group(0)
    expanded = os.path.expanduser(user_expr)
    if expanded == user_expr:
        return word
    new_first = Part(expanded + first.text[len(user_expr) :], first.mode)
    return Word((new_first, *word.parts[1:]))


def expand_assignment_tilde(word: Word) -> Word:
    if not word.parts:
        return word
    first = word.parts[0]
    if first.mode != PLAIN or not first.text.startswith("~"):
        return word
    return expand_initial_tilde(word)


def expand_text(shell, text: str, *, quoted: bool) -> list[Char]:
    result: list[Char] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "$":
            value, i = expand_dollar(shell, text, i, quoted=quoted)
            result.extend(value)
            continue
        if char == "`":
            command, i = read_backtick(text, i + 1)
            output = shell.capture(command).rstrip("\n")
            result.extend(Char(char, quoted) for char in output)
            continue
        result.append(Char(char, quoted))
        i += 1
    return result


def expand_dollar(shell, text: str, index: int, *, quoted: bool) -> tuple[list[Char], int]:
    if index + 1 >= len(text):
        return [Char("$", quoted)], index + 1
    marker = text[index + 1]

    if marker == "(":
        command, end = read_command_substitution(text, index + 2)
        output = shell.capture(command).rstrip("\n")
        return [Char(char, quoted) for char in output], end

    if marker == "{":
        body, end = read_braced_parameter(text, index + 2)
        value = expand_braced_parameter(shell, body, quoted=quoted)
        return [Char(char, quoted) for char in value], end

    if marker in "?$#@*":
        return chars_for_value(shell.get_parameter(marker), quoted), index + 2

    if marker.isdigit():
        return chars_for_value(shell.get_parameter(marker), quoted), index + 2

    if marker.isalpha() or marker == "_":
        end = index + 2
        while end < len(text) and (text[end].isalnum() or text[end] == "_"):
            end += 1
        return chars_for_value(shell.get_parameter(text[index + 1 : end]), quoted), end

    return [Char("$", quoted)], index + 1


def chars_for_value(value: str, quoted: bool) -> list[Char]:
    return [Char(char, quoted) for char in value]


def expand_braced_parameter(shell, body: str, *, quoted: bool) -> str:
    name, op, word = split_parameter_body(body)
    if not name:
        raise ExpansionError(f"bad substitution: ${{{body}}}")

    is_set = shell.parameter_is_set(name)
    value = shell.get_parameter(name)
    is_null = value == ""
    use_colon = op.startswith(":") if op else False
    test_fails = (not is_set) or (use_colon and is_null)

    if not op:
        return value
    if op in {"-", ":-"}:
        return expand_inline(shell, word, quoted=quoted) if test_fails else value
    if op in {"=", ":="}:
        if test_fails:
            replacement = expand_inline(shell, word, quoted=quoted)
            if not is_name(name):
                raise ExpansionError(f"cannot assign to parameter {name}")
            shell.set_parameter(name, replacement)
            return replacement
        return value
    if op in {"+", ":+"}:
        return expand_inline(shell, word, quoted=quoted) if not test_fails else ""
    if op in {"?", ":?"}:
        if test_fails:
            message = expand_inline(shell, word, quoted=quoted) or f"{name}: parameter not set"
            raise ExpansionError(message)
        return value
    raise ExpansionError(f"bad substitution: ${{{body}}}")


def split_parameter_body(body: str) -> tuple[str, str, str]:
    if not body:
        return "", "", ""

    if body[0] in "?$#@*" or body[0].isdigit():
        name = body[0]
        rest = body[1:]
    else:
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", body)
        if not match:
            return "", "", ""
        name = match.group(0)
        rest = body[len(name) :]

    for op in (":-", ":=", ":+", ":?", "-", "=", "+", "?"):
        if rest.startswith(op):
            return name, op, rest[len(op) :]
    if rest:
        return "", "", ""
    return name, "", ""


def expand_inline(shell, text: str, *, quoted: bool) -> str:
    return chars_to_text(expand_text(shell, text, quoted=quoted))


def read_braced_parameter(text: str, start: int) -> tuple[str, int]:
    i = start
    depth = 1
    quote: str | None = None
    escaped = False
    body: list[str] = []
    while i < len(text):
        char = text[i]
        if escaped:
            body.append(char)
            escaped = False
            i += 1
            continue
        if char == "\\":
            body.append(char)
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            body.append(char)
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            body.append(char)
            i += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return "".join(body), i + 1
        body.append(char)
        i += 1
    raise ExpansionError("unterminated parameter expansion")


def read_command_substitution(text: str, start: int) -> tuple[str, int]:
    i = start
    depth = 1
    quote: str | None = None
    escaped = False
    body: list[str] = []
    while i < len(text):
        char = text[i]
        if escaped:
            body.append(char)
            escaped = False
            i += 1
            continue
        if char == "\\":
            body.append(char)
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            body.append(char)
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            body.append(char)
            i += 1
            continue
        if char == "$" and i + 1 < len(text) and text[i + 1] == "(":
            depth += 1
            body.append("$(")
            i += 2
            continue
        if char == ")":
            depth -= 1
            if depth == 0:
                return "".join(body), i + 1
        body.append(char)
        i += 1
    raise ExpansionError("unterminated command substitution")


def read_backtick(text: str, start: int) -> tuple[str, int]:
    i = start
    escaped = False
    body: list[str] = []
    while i < len(text):
        char = text[i]
        if escaped:
            body.append(char)
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if char == "`":
            return "".join(body), i + 1
        body.append(char)
        i += 1
    raise ExpansionError("unterminated command substitution")


def split_fields(shell, chars: list[Char], had_quoted: bool) -> list[list[Char]]:
    if not chars:
        return [[]] if had_quoted else []

    ifs = shell.get_parameter("IFS")
    if ifs == "":
        return [chars]
    ifs_whitespace = {char for char in ifs if char.isspace()}
    ifs_other = set(ifs) - ifs_whitespace

    fields: list[list[Char]] = []
    current: list[Char] = []
    i = 0
    while i < len(chars):
        char = chars[i]
        if not char.quoted and char.value in ifs:
            if current or char.value in ifs_other:
                fields.append(current)
                current = []
            while i + 1 < len(chars) and not chars[i + 1].quoted and chars[i + 1].value in ifs_whitespace:
                i += 1
            i += 1
            continue
        current.append(char)
        i += 1
    if current:
        fields.append(current)
    return fields


def has_unquoted_glob(chars: list[Char]) -> bool:
    return any((not char.quoted) and char.value in "*?[" for char in chars)


def chars_to_text(chars: list[Char]) -> str:
    return "".join(char.value for char in chars)

