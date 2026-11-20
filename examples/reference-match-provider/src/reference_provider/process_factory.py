"""Construct the configured matcher inside the installed scientific worker."""

from collections.abc import Mapping

from .binding import ReferenceConfig, ReferenceMatcher
from . import science


def construct(*, startup: Mapping) -> ReferenceMatcher:
    """Retain settings and references; the SDK initializer builds the index."""
    return ReferenceMatcher(
        configuration=ReferenceConfig(
            settings=science.MatchSettings(**startup["settings"]),
            references=science.ReferenceSet(entries=tuple(
                science.Reference(label=entry["label"], values=tuple(entry["values"]))
                for entry in startup["references"]
            )),
        )
    )
