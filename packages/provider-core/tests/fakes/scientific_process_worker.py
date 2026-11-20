"""Expose only staged test fixtures to the isolated, installed science worker."""

from pathlib import Path
import sys

from provider_core.container_guard import require_container

require_container()


if __name__ == "__main__":
    # SDK qualification copies tests outside the checkout. Isolated Python does
    # not inherit unittest's parent-only addition of this staged root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

    from tests.fakes.scientific_process import run_worker

    run_worker()
