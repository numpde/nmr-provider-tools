"""Stage explicit deployment input without changing provider startup or science."""

from contextlib import contextmanager
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import tomllib
from unittest.mock import patch

import tomli_w


@contextmanager
def staged_configuration(*, provider, source):
    """Restore the provider path before removing this context's copied document."""
    with TemporaryDirectory() as directory:
        configuration = Path(directory) / "provider.toml"
        shutil.copyfile(source, configuration)
        with patch.object(provider, "PROVIDER_CONFIG_PATH", configuration):
            yield configuration


def configure_backend(configuration, backend):
    """Change the raw deployment document without depending on TOML formatting."""
    document = tomllib.loads(configuration.read_text(encoding="utf-8"))
    document["execution"]["backend"] = backend
    configuration.write_text(tomli_w.dumps(document), encoding="utf-8")
    return document
