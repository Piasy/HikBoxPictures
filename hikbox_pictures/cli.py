from __future__ import annotations

from collections.abc import Sequence

from hikbox.cli import main


def cli_entry(argv: Sequence[str] | None = None) -> int:
    return main(argv)
