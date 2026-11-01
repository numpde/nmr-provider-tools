"""Run the actual probe payload without image-specific Python startup hooks."""

import ast
from pathlib import Path
import subprocess
import tempfile
import unittest
import venv

from helpers import IMAGE
from provider_deploy.spec import PROBE_SCRIPT, ProbeSpec


class PythonRuntimeTests(unittest.TestCase):
    def test_probe_skips_site_startup_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / "environment"
            # Symlinks run the installed interpreter even when /tmp is noexec.
            venv.create(environment, symlinks=True, with_pip=False)
            interpreter = str(environment / "bin" / "python")
            site_packages, = environment.glob("lib/python*/site-packages")
            sentinel = root / "site-started"
            (site_packages / "probe.pth").write_text(
                "import pathlib, sys; "
                f"pathlib.Path({str(sentinel)!r}).write_text(repr(sys.argv), encoding='utf-8')\n",
                encoding="utf-8",
            )
            token = "b" * 32
            spec = ProbeSpec(token, IMAGE, interpreter, 1000, 2000, "default", 1)

            # Control: isolated mode alone still runs the venv's executable .pth hook.
            control = subprocess.run(
                [interpreter, "-I", "-B", "-c", PROBE_SCRIPT, "1", token],
                capture_output=True, text=True, timeout=10, check=True,
            )
            self.assertEqual(("", ""), (control.stdout, control.stderr))
            self.assertEqual(["-c", "1", token], ast.literal_eval(sentinel.read_text()))
            sentinel.unlink()

            result = subprocess.run(
                [spec.interpreter, *spec.command],
                capture_output=True, text=True, timeout=10, check=True,
            )
            self.assertEqual(("", ""), (result.stdout, result.stderr))
            self.assertEqual(("-c", PROBE_SCRIPT, "1", token), spec.command[-4:])
            self.assertFalse(sentinel.exists(), "probe executed a site startup hook before its sleeper")


if __name__ == "__main__":
    unittest.main()
