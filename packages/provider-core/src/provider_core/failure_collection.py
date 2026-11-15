"""Collect independent failures while their owners attempt every required phase."""

from __future__ import annotations

from provider_core.container_guard import require_container

require_container()

from collections.abc import Iterator
from contextlib import contextmanager


def _leaves(*, cause: BaseException) -> Iterator[BaseException]:
    if isinstance(cause, BaseExceptionGroup):
        for child in cause.exceptions:
            yield from _leaves(cause=child)
    else:
        yield cause


class FailureCollector:
    """Attempt explicit phases, then report their original independent failures.

    capture() records a failure and lets the caller attempt the next phase.
    It owns no resources or tasks and does not settle interrupted work. Callers
    retain their existing cleanup order, settlement owners and observation caps.
    Call raise_if_any() after the phases to propagate the collected failures.

    Later captures omit causes already recorded, comparing identity rather than
    messages or equality. Existing exception groups remain unchanged unless an
    overlap requires filtering; subgroup preserves their metadata and original
    leaf exceptions. A lone captured cause is raised directly.
    """

    def __init__(self, *, message: str) -> None:
        self.message = message
        self._causes: list[BaseException] = []
        self._seen: set[int] = set()

    @contextmanager
    def capture(self) -> Iterator[None]:
        """Keep one phase's failure without skipping the caller's later phases."""

        try:
            yield
        except BaseException as cause:
            if isinstance(cause, BaseExceptionGroup):
                # Filtering can rebuild a group even if every leaf matches. Keep
                # the original group whenever no recorded cause overlaps it.
                if any(id(leaf) in self._seen for leaf in _leaves(cause=cause)):
                    # Python 3.12 needs a function rather than a bound method.
                    def unrecorded_leaf(candidate: BaseException) -> bool:
                        return not isinstance(candidate, BaseExceptionGroup) and id(candidate) not in self._seen

                    cause = cause.subgroup(unrecorded_leaf)
            elif id(cause) in self._seen:
                return

            if cause is not None:
                self._causes.append(cause)
                self._seen.update(id(leaf) for leaf in _leaves(cause=cause))

    def raise_if_any(self) -> None:
        """Raise one original cause directly, or group independent failures."""

        if len(self._causes) == 1:
            raise self._causes[0]

        if self._causes:
            # Causes are already explicit in the group; do not print an active
            # caller exception again as implicit context above that same group.
            raise BaseExceptionGroup(self.message, self._causes) from None
