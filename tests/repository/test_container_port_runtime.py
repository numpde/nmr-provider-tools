"""Observe the maintained repository lane's effective kernel grants.

Port of all eight RuntimePostureTests at
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/tests/repository/test_container_posture.py#L18
Identity and numeric resources follow tools/repository.sh: caller ownership,
512 MiB, two CPUs, 64 PIDs and zero swap. The launcher injects expected identity;
reading the process's own UID as the expected value would erase this proof.
Magnet's fixture-install assertion observes temporary build trees in this test
image. Inference-image contents still require that image's separate import lane;
the raw Python test image cannot certify its contents.
"""

from tools.container_guard import require_container

require_container()

import os
import tempfile
import unittest
from pathlib import Path


def mount_record(destination):
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        separator = fields.index("-")
        if fields[4] == destination:
            return fields[separator + 1], set(fields[5].split(",")) | set(fields[separator + 3].split(","))
    raise AssertionError(f"Cannot verify runtime mount posture: {destination} has no mountinfo record.")


def process_status():
    return dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())


class RuntimePostureTests(unittest.TestCase):
    def test_runs_as_explicit_non_root_caller_identity(self):
        for variable, actual in (
            ("NMR_PROVIDER_TOOLS_EXPECTED_UID", os.getuid()),
            ("NMR_PROVIDER_TOOLS_EXPECTED_GID", os.getgid()),
        ):
            expected = os.environ.get(variable)
            self.assertIsNotNone(expected, msg=f"Cannot verify caller identity: the maintained launcher must inject {variable}.")
            self.assertTrue(
                expected.isdecimal(), msg=f"Cannot verify caller identity: {variable} must contain a numeric identity."
            )
            self.assertNotEqual(int(expected), 0)
            self.assertEqual(int(expected), actual, msg=f"Process identity differs from the launcher grant for {variable}.")

    def test_root_is_read_only_and_tmp_is_writable(self):
        self.assertIn("ro", mount_record("/")[1])
        with tempfile.NamedTemporaryFile(dir="/tmp") as probe:
            probe.write(b"expected")
            probe.flush()
            self.assertEqual(b"expected", Path(probe.name).read_bytes())

    def test_has_no_effective_linux_capabilities(self):
        self.assertEqual(0, int(process_status()["CapEff"].strip(), 16))

    def test_kernel_enforces_no_new_privileges(self):
        self.assertEqual("1", process_status()["NoNewPrivs"].strip())

    def test_has_only_loopback_network_interface(self):
        self.assertEqual({"lo"}, {path.name for path in Path("/sys/class/net").iterdir()})

    def test_tmp_is_a_hardened_writable_tmpfs(self):
        filesystem, options = mount_record("/tmp")
        self.assertEqual("tmpfs", filesystem)
        self.assertTrue({"rw", "noexec", "nosuid", "nodev"} <= options)

    def test_cgroup_v2_enforces_resource_bounds(self):
        cgroup = Path("/sys/fs/cgroup")
        for name, expected in (("pids.max", "64"), ("memory.max", "536870912"), ("memory.swap.max", "0")):
            self.assertEqual(
                expected, (cgroup / name).read_text().strip(), msg=f"Cgroup {name} differs from the maintained resource grant."
            )
        quota, period = map(int, (cgroup / "cpu.max").read_text().split())
        self.assertEqual(2 * period, quota, msg="CPU quota differs from the maintained two-CPU grant.")

    def test_fixture_install_retains_no_build_source(self):
        for name in ("/build/models/embedded_fixture", "/build/test-products", "/fixture-stage", "/opt/build"):
            self.assertFalse(Path(name).exists(), msg=f"Build source remains at {name}.")
        directory = Path("/build")
        if directory.exists():
            self.assertFalse(any(path.is_file() for path in directory.rglob("*")))


if __name__ == "__main__":
    unittest.main()
