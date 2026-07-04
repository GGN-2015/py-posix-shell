"""Shared exception types for py-posix-shell."""


class ShellError(Exception):
    """Base class for shell errors."""


class LexerError(ShellError):
    """Raised when shell source cannot be tokenized."""


class ParseError(ShellError):
    """Raised when shell tokens do not form a valid command list."""


class ExpansionError(ShellError):
    """Raised when shell expansion fails."""


class ShellExit(BaseException):
    """Internal control-flow exception for the exit builtin."""

    def __init__(self, status: int = 0) -> None:
        self.status = status
        super().__init__(status)

