"""Run cold identity proofs from the exact hashed bootstrap wheel."""

import hashlib
import pathlib
import sys
import tomllib
import unittest
import urllib.parse


lock = tomllib.loads(pathlib.Path("/bootstrap.pylock.toml").read_text())
(package,) = (package for package in lock["packages"] if package["name"] == "linux-idmap")
(wheel,) = package["wheels"]
identity = pathlib.Path("/wheelhouse") / pathlib.PurePosixPath(urllib.parse.urlsplit(wheel["url"]).path).name
observed_sha256 = hashlib.sha256(identity.read_bytes()).hexdigest()
expected_sha256 = wheel["hashes"]["sha256"]

if not (observed_sha256 == expected_sha256):
    raise ValueError(f"Cannot test acquisition identity: {identity = !s} has {observed_sha256 = }, which differs from {expected_sha256 = }.")

sys.path[:0] = [str(identity), "/source", "/tests"]
unittest.main(module="test_identity", verbosity=2)
