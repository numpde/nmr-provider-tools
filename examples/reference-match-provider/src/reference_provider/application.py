"""Match each query and deliver its report before starting the next query."""


async def run_queries(*, provider, candidates, receive) -> None:
    """Reuse the loaded index; a later failure cannot suppress an earlier report."""

    for candidate in candidates:
        await provider.match(scientific_input=candidate, receive=receive)
