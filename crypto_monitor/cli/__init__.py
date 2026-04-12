"""Command-line interface for crypto_monitor.

This package is the thinnest possible shell over the scheduler and
the lower-layer modules. Parsing and printing live here; every piece
of business logic is reused verbatim from its dedicated module.

Entry point: `python -m crypto_monitor.cli <command> [args]` is wired
through `__main__.py`. Programmatic callers (tests) invoke
`crypto_monitor.cli.main.main(argv)`.
"""

from crypto_monitor.cli.main import main

__all__ = ["main"]
