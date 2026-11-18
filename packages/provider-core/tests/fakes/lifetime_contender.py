"""A separate installed-package process contends for the actual lifetime lock."""

import pathlib
import sys

from provider_core.lifetime import ProviderLifetime


def main():
    (mode, root) = (sys.argv[1], pathlib.Path(sys.argv[2]))
    lifetime = ProviderLifetime(root)

    try:
        lifetime.open()
    except BlockingIOError:
        print("blocked", flush=True)
    else:
        print("acquired", flush=True)

        if mode == "hold":
            sys.stdin.read()
    finally:
        lifetime.close()


if __name__ == "__main__":
    main()
