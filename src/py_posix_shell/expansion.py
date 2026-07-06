"""Shell word expansion."""

from __future__ import annotations

import glob
import ast
import fnmatch
import os
import re
from dataclasses import dataclass

from .errors import ExpansionError, LexerError
from .lexer import DOUBLE, PLAIN, SINGLE, Operator, Part, Word, is_name, lex


@dataclass(frozen=True)
class Char:
    value: str
    quoted: bool = False


def expand_word(shell, word: Word, *, field_split: bool = True, pathname: bool = True) -> list[str]:
    if field_split and word.parts and len(word.parts) == 1 and word.parts[0].mode == DOUBLE and word.parts[0].text == "$@":
        return shell.positional.copy()

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
        if pathname and not shell.options.get("noglob", False) and has_unquoted_glob(field):
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


def expand_here_document(shell, body: str) -> str:
    return chars_to_text(expand_text(shell, body, quoted=False))


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

    if marker == "(" and index + 2 < len(text) and text[index + 2] == "(":
        expression, end = read_arithmetic_expansion(text, index + 3)
        return chars_for_value(str(eval_arithmetic(shell, expression)), quoted), end

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
        return chars_for_value(shell.get_parameter(text[index + 1 : end], strict=True), quoted), end

    return [Char("$", quoted)], index + 1


def chars_for_value(value: str, quoted: bool) -> list[Char]:
    return [Char(char, quoted) for char in value]


def expand_braced_parameter(shell, body: str, *, quoted: bool) -> str:
    if body.startswith("#"):
        name = body[1:]
        if not name:
            raise ExpansionError(f"bad substitution: ${{{body}}}")
        return str(len(shell.get_parameter(name, strict=True)))

    name, op, word = split_parameter_body(body)
    if not name:
        raise ExpansionError(f"bad substitution: ${{{body}}}")

    is_set = shell.parameter_is_set(name)
    value = shell.get_parameter(name)
    is_null = value == ""
    use_colon = op.startswith(":") if op else False
    test_fails = (not is_set) or (use_colon and is_null)

    if not op:
        if not is_set and shell.options.get("nounset", False):
            raise ExpansionError(f"{name}: parameter not set")
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
    if op in {"#", "##", "%", "%%"}:
        pattern = expand_inline(shell, word, quoted=quoted)
        return remove_pattern(value, pattern, op)
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

    for op in (":-", ":=", ":+", ":?", "##", "%%", "-", "=", "+", "?", "#", "%"):
        if rest.startswith(op):
            return name, op, rest[len(op) :]
    if rest:
        return "", "", ""
    return name, "", ""


def expand_inline(shell, text: str, *, quoted: bool) -> str:
    try:
        tokens = lex(text)
    except LexerError:
        tokens = []
    if tokens:
        expanded: list[str] = []
        for token in tokens:
            if isinstance(token, Word):
                chars, _had_quoted = expand_word_to_chars(shell, token)
                expanded.append(chars_to_text(chars))
            elif isinstance(token, Operator):
                expanded.append(token.value)
        return " ".join(expanded)
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
            if quote == '"' and char == "$" and i + 1 < len(text) and text[i + 1] == "(":
                depth += 1
                body.append("$(")
                i += 2
                continue
            if quote == '"' and char == ")" and depth > 1:
                depth -= 1
                body.append(char)
                i += 1
                continue
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


def read_arithmetic_expansion(text: str, start: int) -> tuple[str, int]:
    i = start
    depth = 1
    body: list[str] = []
    quote: str | None = None
    escaped = False
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
        if text.startswith("((", i):
            depth += 1
            body.append("((")
            i += 2
            continue
        if text.startswith("))", i):
            depth -= 1
            i += 2
            if depth == 0:
                return "".join(body), i
            body.append("))")
            continue
        body.append(char)
        i += 1
    raise ExpansionError("unterminated arithmetic expansion")


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


def remove_pattern(value: str, pattern: str, op: str) -> str:
    if op == "#":
        cuts = range(0, len(value) + 1)
        for cut in cuts:
            if fnmatch.fnmatchcase(value[:cut], pattern):
                return value[cut:]
    if op == "##":
        cuts = range(len(value), -1, -1)
        for cut in cuts:
            if fnmatch.fnmatchcase(value[:cut], pattern):
                return value[cut:]
    if op == "%":
        cuts = range(len(value), -1, -1)
        for cut in cuts:
            if fnmatch.fnmatchcase(value[cut:], pattern):
                return value[:cut]
    if op == "%%":
        cuts = range(0, len(value) + 1)
        for cut in cuts:
            if fnmatch.fnmatchcase(value[cut:], pattern):
                return value[:cut]
    return value


def eval_arithmetic(shell, expression: str) -> int:
    expression = expression.strip()
    side_effect = eval_arithmetic_side_effect(shell, expression)
    if side_effect is not None:
        return side_effect
    translated = translate_arithmetic(expression)
    try:
        tree = ast.parse(translated, mode="eval")
    except SyntaxError as exc:
        raise ExpansionError(f"bad arithmetic expression: {expression}") from exc
    return int(eval_arithmetic_node(shell, tree.body))


def eval_arithmetic_side_effect(shell, expression: str) -> int | None:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*(\+\+|--)", expression)
    if match:
        name, op = match.groups()
        old = arithmetic_parameter(shell, name)
        shell.set_parameter(name, str(old + (1 if op == "++" else -1)))
        return old

    match = re.fullmatch(r"(\+\+|--)\s*([A-Za-z_][A-Za-z0-9_]*)", expression)
    if match:
        op, name = match.groups()
        new = arithmetic_parameter(shell, name) + (1 if op == "++" else -1)
        shell.set_parameter(name, str(new))
        return new

    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*([+\-*/%&|^]?=|<<=|>>=)\s*(.+)", expression)
    if not match:
        return None
    name, op, rhs = match.groups()
    right = eval_arithmetic(shell, rhs)
    if op == "=":
        value = right
    else:
        left = arithmetic_parameter(shell, name)
        if op == "+=":
            value = left + right
        elif op == "-=":
            value = left - right
        elif op == "*=":
            value = left * right
        elif op == "/=":
            if right == 0:
                raise ExpansionError("division by zero")
            value = int(left / right)
        elif op == "%=":
            if right == 0:
                raise ExpansionError("division by zero")
            value = left % right
        elif op == "&=":
            value = left & right
        elif op == "|=":
            value = left | right
        elif op == "^=":
            value = left ^ right
        elif op == "<<=":
            value = left << right
        elif op == ">>=":
            value = left >> right
        else:
            raise ExpansionError(f"bad arithmetic assignment: {expression}")
    shell.set_parameter(name, str(value))
    return value


def arithmetic_parameter(shell, name: str) -> int:
    value = shell.get_parameter(name) or "0"
    try:
        return int(value, 0)
    except ValueError:
        return 0


def translate_arithmetic(expression: str) -> str:
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"(?<![=!<>])!(?!=)", " not ", expression)
    return expression


def eval_arithmetic_node(shell, node: ast.AST) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, bool)):
        return int(node.value)
    if isinstance(node, ast.Name):
        value = shell.get_parameter(node.id) or "0"
        try:
            return int(value, 0)
        except ValueError:
            return 0
    if isinstance(node, ast.UnaryOp):
        value = eval_arithmetic_node(shell, node.operand)
        if isinstance(node.op, ast.UAdd):
            return value
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.Invert):
            return ~value
        if isinstance(node.op, ast.Not):
            return 0 if value else 1
    if isinstance(node, ast.BinOp):
        left = eval_arithmetic_node(shell, node.left)
        right = eval_arithmetic_node(shell, node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv)):
            if right == 0:
                raise ExpansionError("division by zero")
            return int(left / right)
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ExpansionError("division by zero")
            return left % right
        if isinstance(node.op, ast.LShift):
            return left << right
        if isinstance(node.op, ast.RShift):
            return left >> right
        if isinstance(node.op, ast.BitAnd):
            return left & right
        if isinstance(node.op, ast.BitOr):
            return left | right
        if isinstance(node.op, ast.BitXor):
            return left ^ right
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for value_node in node.values:
                if eval_arithmetic_node(shell, value_node) == 0:
                    return 0
            return 1
        if isinstance(node.op, ast.Or):
            for value_node in node.values:
                if eval_arithmetic_node(shell, value_node) != 0:
                    return 1
            return 0
    if isinstance(node, ast.Compare):
        left = eval_arithmetic_node(shell, node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = eval_arithmetic_node(shell, comparator)
            ok = (
                (isinstance(op, ast.Eq) and left == right)
                or (isinstance(op, ast.NotEq) and left != right)
                or (isinstance(op, ast.Lt) and left < right)
                or (isinstance(op, ast.LtE) and left <= right)
                or (isinstance(op, ast.Gt) and left > right)
                or (isinstance(op, ast.GtE) and left >= right)
            )
            if not ok:
                return 0
            left = right
        return 1
    raise ExpansionError("unsupported arithmetic expression")
