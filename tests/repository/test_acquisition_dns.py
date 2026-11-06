"""The selected link's DNS wins over an unrelated host-global resolver."""

from tools.container_guard import require_container

require_container()

from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from acquisition_tool import dns


class ResolverCaptureTests(unittest.TestCase):
    def test_networkmanager_link_dns_wins_without_consulting_global_resolvers(self):
        response = SimpleNamespace(returncode=0, stdout="192.0.2.53\n192.0.2.54\n192.0.2.53\n")
        with patch.object(dns.socket, "if_nametoindex", return_value=2), \
                patch.object(dns.subprocess, "run", return_value=response) as run, \
                patch.object(Path, "read_text") as read:
            self.assertEqual(("192.0.2.53", "192.0.2.54"), dns.capture_dns_servers("wlan0"))
        self.assertEqual(1, run.call_count)
        self.assertEqual(["nmcli", "--get-values", "IP4.DNS", "device", "show", "wlan0"], run.call_args.args[0])
        read.assert_not_called()

    def test_resolved_link_dns_is_used_when_networkmanager_has_no_usable_set(self):
        responses = [SimpleNamespace(returncode=0, stdout=""),
                     SimpleNamespace(returncode=0, stdout="Link 2 (wlan0): 192.0.2.53 2001:db8::53\n")]
        with patch.object(dns.socket, "if_nametoindex", return_value=2), \
                patch.object(dns.subprocess, "run", side_effect=responses), \
                patch.object(Path, "read_text") as read:
            self.assertEqual(("192.0.2.53",), dns.capture_dns_servers("wlan0"))
        read.assert_not_called()

    def test_unmanaged_link_retains_configured_global_resolvers(self):
        with patch.object(dns.socket, "if_nametoindex", return_value=2), \
                patch.object(dns.subprocess, "run", side_effect=FileNotFoundError), \
                patch.object(Path, "read_text", side_effect=[OSError(), "nameserver 127.0.0.53\nnameserver 192.0.2.53\n"]):
            self.assertEqual(("192.0.2.53",), dns.capture_dns_servers("wlan0"))

    def test_system_route_uses_existing_global_configuration_without_manager_calls(self):
        with patch.object(dns.subprocess, "run") as run, \
                patch.object(Path, "read_text", return_value="nameserver 192.0.2.53\n"):
            self.assertEqual(("192.0.2.53",), dns.capture_dns_servers("system"))
        run.assert_not_called()

    def test_manager_timeout_is_explicit_and_does_not_choose_another_source(self):
        with patch.object(dns.socket, "if_nametoindex", return_value=2), \
                patch.object(dns.subprocess, "run", side_effect=subprocess.TimeoutExpired("nmcli", 3)), \
                patch.object(Path, "read_text") as read, \
                self.assertRaisesRegex(dns.ResolverCaptureFailed, "three seconds"):
            dns.capture_dns_servers("wlan0")
        read.assert_not_called()

    def test_capture_file_is_private_and_cannot_replace_existing_operation_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "resolv.conf"
            with patch("sys.argv", ["dns.py", "--interface", "wlan0", "--output", str(output)]), \
                    patch.object(dns, "capture_dns_servers", return_value=("192.0.2.53",)):
                dns.main()
                self.assertEqual("nameserver 192.0.2.53\n", output.read_text())
                self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
                with self.assertRaises(SystemExit):
                    dns.main()
            self.assertEqual("nameserver 192.0.2.53\n", output.read_text())
