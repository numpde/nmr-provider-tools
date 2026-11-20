"""Keep repository findings available to both unittest and its HTML reporter."""

from tools.container_guard import require_container

require_container()


class FindingsFailure(AssertionError):
    """Retain the diagnostic list that caused an inventory check to fail."""

    def __init__(self, *, findings: list[str]) -> None:
        self.findings = findings

        super().__init__("\n".join(findings))


def assert_no_findings(findings: list[str]) -> None:
    """Fail with every finding and let reporters count the same list."""

    if not (len(findings) == 0):
        raise FindingsFailure(findings=findings)
