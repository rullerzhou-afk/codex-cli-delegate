"""Shared core error type for the delegate runtime.

``claude_task`` can be executed either as the imported ``claude_task`` module
(via ``delegate.py``) or directly as ``__main__``. Defining the operator-visible
error type once here keeps a single ``CliError`` class across those instances,
so raising and catching always agree. It also lets the extracted modules raise
the public error without importing the whole composition root.
"""


class CliError(Exception):
    """An operator-visible failure reported as compact JSON."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra
