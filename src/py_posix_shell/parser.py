"""Parser for the py-posix-shell command language."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from .errors import ParseError
from .lexer import Operator, Part, Token, Word, is_name, lex


@dataclass(frozen=True)
class Redirection:
    fd: int
    op: str
    target: Word


@dataclass(frozen=True)
class SimpleCommand:
    assignments: tuple[tuple[str, Word], ...] = field(default_factory=tuple)
    words: tuple[Word, ...] = field(default_factory=tuple)
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class IfCommand:
    condition: "Script"
    then_body: "Script"
    elifs: tuple[tuple["Script", "Script"], ...] = field(default_factory=tuple)
    else_body: "Script | None" = None
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LoopCommand:
    kind: str
    condition: "Script"
    body: "Script"
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ForCommand:
    name: str
    words: tuple[Word, ...] | None
    body: "Script"
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CaseItem:
    patterns: tuple[Word, ...]
    body: "Script"


@dataclass(frozen=True)
class CaseCommand:
    word: Word
    items: tuple[CaseItem, ...]
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GroupCommand:
    body: "Script"
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SubshellCommand:
    body: "Script"
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FunctionDef:
    name: str
    body: "Command"
    redirections: tuple[Redirection, ...] = field(default_factory=tuple)


Command: TypeAlias = (
    SimpleCommand
    | IfCommand
    | LoopCommand
    | ForCommand
    | CaseCommand
    | GroupCommand
    | SubshellCommand
    | FunctionDef
)


@dataclass(frozen=True)
class Pipeline:
    commands: tuple[Command, ...]
    negated: bool = False


@dataclass(frozen=True)
class AndOrList:
    first: Pipeline
    rest: tuple[tuple[str, Pipeline], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ListItem:
    command: AndOrList
    background: bool = False


@dataclass(frozen=True)
class Script:
    items: tuple[ListItem, ...]


def parse(source: str) -> Script:
    return Parser(lex(source)).parse()


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> Script:
        script = self._parse_list(stop_words=set(), stop_ops=set())
        if not self._at_end():
            raise ParseError(f"unexpected token {self._describe(self._peek())}")
        return script

    def _parse_list(self, *, stop_words: set[str], stop_ops: set[str]) -> Script:
        items: list[ListItem] = []
        self._skip_separators()
        while not self._at_end() and not self._at_stop(stop_words, stop_ops):
            item = self._parse_and_or(stop_words=stop_words, stop_ops=stop_ops)
            background = self._consume_operator("&")
            items.append(ListItem(item, background))
            if self._at_end() or self._at_stop(stop_words, stop_ops):
                break
            if not self._consume_operator(";"):
                token = self._peek()
                raise ParseError(f"expected command separator before {self._describe(token)}")
            self._skip_separators()
        return Script(tuple(items))

    def _parse_and_or(self, *, stop_words: set[str], stop_ops: set[str]) -> AndOrList:
        first = self._parse_pipeline(stop_words=stop_words, stop_ops=stop_ops)
        rest: list[tuple[str, Pipeline]] = []
        while True:
            if self._consume_operator("&&"):
                rest.append(("&&", self._parse_pipeline(stop_words=stop_words, stop_ops=stop_ops)))
            elif self._consume_operator("||"):
                rest.append(("||", self._parse_pipeline(stop_words=stop_words, stop_ops=stop_ops)))
            else:
                break
        return AndOrList(first, tuple(rest))

    def _parse_pipeline(self, *, stop_words: set[str], stop_ops: set[str]) -> Pipeline:
        negated = self._consume_word_text("!")
        commands = [self._parse_command(stop_words=stop_words, stop_ops=stop_ops)]
        while self._consume_operator("|"):
            if self._at_end() or self._peek_operator() in {";", "&", "&&", "||", "|", ";;", ")"}:
                raise ParseError("expected command after pipe")
            commands.append(self._parse_command(stop_words=stop_words, stop_ops=stop_ops))
        return Pipeline(tuple(commands), negated)

    def _parse_command(self, *, stop_words: set[str], stop_ops: set[str]) -> Command:
        if self._is_function_definition():
            return self._parse_function_definition()

        word = self._peek_word_text()
        if word == "if":
            return self._parse_if()
        if word in {"while", "until"}:
            return self._parse_loop()
        if word == "for":
            return self._parse_for()
        if word == "case":
            return self._parse_case()
        if word == "{":
            return self._parse_group()
        if self._peek_operator() == "(":
            return self._parse_subshell()
        return self._parse_simple_command(stop_words=stop_words, stop_ops=stop_ops)

    def _parse_if(self) -> IfCommand:
        self._expect_word_text("if")
        self._skip_separators()
        condition = self._parse_list(stop_words={"then"}, stop_ops=set())
        self._expect_word_text("then")
        self._skip_separators()
        then_body = self._parse_list(stop_words={"elif", "else", "fi"}, stop_ops=set())
        elifs: list[tuple[Script, Script]] = []
        while self._consume_word_text("elif"):
            self._skip_separators()
            elif_condition = self._parse_list(stop_words={"then"}, stop_ops=set())
            self._expect_word_text("then")
            self._skip_separators()
            elif_body = self._parse_list(stop_words={"elif", "else", "fi"}, stop_ops=set())
            elifs.append((elif_condition, elif_body))
        else_body = None
        if self._consume_word_text("else"):
            self._skip_separators()
            else_body = self._parse_list(stop_words={"fi"}, stop_ops=set())
        self._expect_word_text("fi")
        return IfCommand(condition, then_body, tuple(elifs), else_body, self._parse_redirections())

    def _parse_loop(self) -> LoopCommand:
        kind = self._consume_word().text
        self._skip_separators()
        condition = self._parse_list(stop_words={"do"}, stop_ops=set())
        self._expect_word_text("do")
        self._skip_separators()
        body = self._parse_list(stop_words={"done"}, stop_ops=set())
        self._expect_word_text("done")
        return LoopCommand(kind, condition, body, self._parse_redirections())

    def _parse_for(self) -> ForCommand:
        self._expect_word_text("for")
        self._skip_separators()
        name_token = self._consume_word()
        name = name_token.text
        if not is_name(name):
            raise ParseError(f"bad for loop variable name: {name}")

        words: list[Word] | None = None
        if self._consume_word_text("in"):
            words = []
            while not self._at_end() and self._peek_operator() not in {";"} and self._peek_word_text() != "do":
                token = self._consume_word()
                words.append(token)
            self._consume_operator(";")
        else:
            self._consume_operator(";")

        self._skip_separators()
        self._expect_word_text("do")
        self._skip_separators()
        body = self._parse_list(stop_words={"done"}, stop_ops=set())
        self._expect_word_text("done")
        return ForCommand(name, None if words is None else tuple(words), body, self._parse_redirections())

    def _parse_case(self) -> CaseCommand:
        self._expect_word_text("case")
        self._skip_separators()
        word = self._consume_word()
        self._expect_word_text("in")
        self._skip_separators()
        items: list[CaseItem] = []
        while not self._at_end() and self._peek_word_text() != "esac":
            self._consume_operator("(")
            patterns: list[Word] = [self._consume_word()]
            while self._consume_operator("|"):
                patterns.append(self._consume_word())
            self._expect_operator(")")
            self._skip_separators()
            body = self._parse_list(stop_words={"esac"}, stop_ops={";;"})
            items.append(CaseItem(tuple(patterns), body))
            self._consume_operator(";;")
            self._skip_separators()
        self._expect_word_text("esac")
        return CaseCommand(word, tuple(items), self._parse_redirections())

    def _parse_group(self) -> GroupCommand:
        self._expect_word_text("{")
        self._skip_separators()
        body = self._parse_list(stop_words={"}"}, stop_ops=set())
        self._expect_word_text("}")
        return GroupCommand(body, self._parse_redirections())

    def _parse_subshell(self) -> SubshellCommand:
        self._expect_operator("(")
        self._skip_separators()
        body = self._parse_list(stop_words=set(), stop_ops={")"})
        self._expect_operator(")")
        return SubshellCommand(body, self._parse_redirections())

    def _parse_function_definition(self) -> FunctionDef:
        name = self._consume_word().text
        self._expect_operator("(")
        self._expect_operator(")")
        self._skip_separators()
        body = self._parse_command(stop_words=set(), stop_ops=set())
        return FunctionDef(name, body, self._parse_redirections())

    def _parse_simple_command(self, *, stop_words: set[str], stop_ops: set[str]) -> SimpleCommand:
        assignments: list[tuple[str, Word]] = []
        words: list[Word] = []
        redirections: list[Redirection] = []
        seen_command_word = False

        while not self._at_end() and not self._at_stop(stop_words, stop_ops):
            token = self._peek()
            if isinstance(token, Word) and self._is_array_append_assignment(token) and not seen_command_word:
                assignments.append(self._parse_array_append_assignment())
                continue
            if isinstance(token, Operator):
                redir = parse_redirection_operator(token.value)
                if redir is None:
                    break
                self.pos += 1
                target = self._consume_word()
                redirections.append(Redirection(redir[0], redir[1], target))
                continue

            self.pos += 1
            assignment = token.split_assignment()
            if assignment is not None and not seen_command_word:
                assignments.append(assignment)
            else:
                seen_command_word = True
                words.append(token)

        if not assignments and not words and not redirections:
            token = self._peek() if not self._at_end() else None
            raise ParseError(f"expected command, got {self._describe(token)}")
        return SimpleCommand(tuple(assignments), tuple(words), tuple(redirections))

    def _is_array_append_assignment(self, token: Word) -> bool:
        return (
            token.text.endswith("+=")
            and is_name(token.text[:-2])
            and self._peek_operator(1) == "("
        )

    def _parse_array_append_assignment(self) -> tuple[str, Word]:
        name = self._consume_word().text[:-2]
        self._expect_operator("(")
        values: list[str] = []
        while not self._at_end() and self._peek_operator() != ")":
            values.append(self._consume_word().text)
        self._expect_operator(")")
        return name, Word((Part(" ".join(values)),))

    def _parse_redirections(self) -> tuple[Redirection, ...]:
        redirections: list[Redirection] = []
        while not self._at_end():
            op = self._peek_operator()
            redir = parse_redirection_operator(op or "")
            if redir is None:
                break
            self.pos += 1
            target = self._consume_word()
            redirections.append(Redirection(redir[0], redir[1], target))
        return tuple(redirections)

    def _consume_word(self) -> Word:
        if self._at_end():
            raise ParseError("expected word, got end of input")
        token = self._peek()
        if isinstance(token, Word):
            self.pos += 1
            return token
        raise ParseError(f"expected word, got {self._describe(token)}")

    def _consume_word_text(self, value: str) -> bool:
        if self._peek_word_text() == value:
            self.pos += 1
            return True
        return False

    def _expect_word_text(self, value: str) -> None:
        if not self._consume_word_text(value):
            token = self._peek() if not self._at_end() else None
            raise ParseError(f"expected {value!r}, got {self._describe(token)}")

    def _consume_operator(self, value: str) -> bool:
        if self._peek_operator() == value:
            self.pos += 1
            return True
        return False

    def _expect_operator(self, value: str) -> None:
        if not self._consume_operator(value):
            token = self._peek() if not self._at_end() else None
            raise ParseError(f"expected {value!r}, got {self._describe(token)}")

    def _skip_separators(self) -> None:
        while self._consume_operator(";"):
            pass

    def _at_stop(self, stop_words: set[str], stop_ops: set[str]) -> bool:
        if self._at_end():
            return True
        op = self._peek_operator()
        if op is not None:
            return op in stop_ops
        word = self._peek_word_text()
        return word in stop_words

    def _is_function_definition(self) -> bool:
        token = self._peek()
        if not isinstance(token, Word) or not is_name(token.text):
            return False
        return self._peek_operator(1) == "(" and self._peek_operator(2) == ")"

    def _peek_word_text(self, offset: int = 0) -> str | None:
        if self.pos + offset >= len(self.tokens):
            return None
        token = self.tokens[self.pos + offset]
        if isinstance(token, Word):
            return token.text
        return None

    def _peek_operator(self, offset: int = 0) -> str | None:
        if self.pos + offset >= len(self.tokens):
            return None
        token = self.tokens[self.pos + offset]
        if isinstance(token, Operator):
            return token.value
        return None

    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _at_end(self) -> bool:
        return self.pos >= len(self.tokens)

    @staticmethod
    def _describe(token: Token | None) -> str:
        if token is None:
            return "end of input"
        if isinstance(token, Operator):
            return repr(token.value)
        return repr(token.text)


def parse_redirection_operator(value: str) -> tuple[int, str] | None:
    if not value:
        return None

    i = 0
    while i < len(value) and value[i].isdigit():
        i += 1

    prefix = value[:i]
    op = value[i:]
    if op not in {"<", ">", ">>", ">&", "<&", ">|", "<<", "<<-", "<>"}:
        return None

    if prefix:
        fd = int(prefix)
    elif op in {"<", "<&", "<<", "<<-", "<>"}:
        fd = 0
    else:
        fd = 1
    return fd, op
