"""Admit filenames recognized by the standalone tool's pylock workflow."""

import reprlib


def require_pylock_filename(filename: str) -> None:
    """Require the standard pylock basename or a named pylock variant."""

    if not (
        (filename == "pylock.toml") or
        (filename.startswith("pylock.") and filename.endswith(".toml"))
    ):
        raise ValueError(f"{reprlib.repr(filename)=} should be pylock.toml or pylock.NAME.toml")
