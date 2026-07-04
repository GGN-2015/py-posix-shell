"""Parser for the py-posix-shell command language."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ParseError
from .lexer import Operator, Token, Word, lex


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
class Pipeline:
    commands: tuple[SimpleCommand, ...]


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
        items: list[ListItem] = []
        self._skip_separators()
        while not self._at_end():
            item = self._parse_and_or()
            background = self._consume_operator("&")
            items.append(ListItem(item, background))
            if self._at_end():
                break
            if not self._consume_operator(";"):
                token = self._peek()
                raise ParseError(f"expected command separator before {self._describe(token)}")
            self._skip_separators()
        return Script(tuple(items))

    def _parse_and_or(self) -> AndOrList:
        first = self._parse_pipeline()
        rest: list[tuple[str, Pipeline]] = []
        while True:
            if self._consume_operator("&&"):
                rest.append(("&&", self._parse_pipeline()))
            elif self._consume_operator("||"):
                rest.append(("||", self._parse_pipeline()))
            else:
                break
        return AndOrList(first, tuple(rest))

    def _parse_pipeline(self) -> Pipeline:
        commands = [self._parse_simple_command()]
        while self._consume_operator("|"):
            if self._at_end() or self._peek_operator() in {";", "&", "&&", "||", "|"}:
                raise ParseError("expected command after pipe")
            commands.append(self._parse_simple_command())
        return Pipeline(tuple(commands))

    def _parse_simple_command(self) -> SimpleCommand:
        assignments: list[tuple[str, Word]] = []
        words: list[Word] = []
        redirections: list[Redirection] = []
        seen_command_word = False

        while not self._at_end():
            token = self._peek()
            if isinstance(token, Operator):
                redir = parse_redirection_operator(token.value)
                if redir is None:
                    break
                self.pos += 1
                target = self._consume_word()
                if target is None:
                    raise ParseError(f"expected redirection target after {token.value}")
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
            token = self._peek()
            raise ParseError(f"expected command, got {self._describe(token)}")
        return SimpleCommand(tuple(assignments), tuple(words), tuple(redirections))

    def _consume_word(self) -> Word | None:
        if self._at_end():
            return None
        token = self._peek()
        if isinstance(token, Word):
            self.pos += 1
            return token
        return None

    def _consume_operator(self, value: str) -> bool:
        if self._peek_operator() == value:
            self.pos += 1
            return True
        return False

    def _skip_separators(self) -> None:
        while self._consume_operator(";"):
            pass

    def _peek_operator(self) -> str | None:
        if self._at_end():
            return None
        token = self._peek()
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
    if op not in {"<", ">", ">>", ">&", "<&", ">|", "<<"}:
        return None

    if prefix:
        fd = int(prefix)
    elif op in {"<", "<&", "<<"}:
        fd = 0
    else:
        fd = 1
    return fd, op
