"""Project a pinned requirements closure to URL-bearing artifact metadata."""

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import tomli_w
from packaging.markers import default_environment
from packaging.pylock import Pylock, PylockSelectError, PylockValidationError
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version
from wheelhouse_tool.network import request as network_request, retry


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("Cannot locate artifacts: index redirected away from HTTPS.")
        return super().redirect_request(request, fp, code, message, headers, url)


def read_requirements(path, configured_index=None):
    """Read a flat pinned closure; this does not resolve dependencies."""
    records = []
    index = configured_index
    source = path.read_bytes()
    for raw in source.decode("utf-8").splitlines():
        line = raw.strip().removesuffix("\\").rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--index-url "):
            declared = line.split(None, 1)[1]
            if index is not None and index.rstrip("/") != declared.rstrip("/"):
                raise ValueError("Cannot locate artifacts: supplied and locked index URLs differ.")
            index = declared
        elif line.startswith("--hash=sha256:"):
            checksum = line.removeprefix("--hash=sha256:")
            if not records or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("Cannot locate artifacts: a lock hash is malformed or misplaced.")
            records[-1][1].add(checksum)
        else:
            requirement = Requirement(line)
            if requirement.marker is not None:
                raise ValueError("Cannot locate artifacts: use a target-specific lock without markers.")
            if requirement.extras:
                raise ValueError("Cannot locate artifacts: use a fully expanded lock without extras.")
            if requirement.url is None:
                specifiers = list(requirement.specifier)
                if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
                    raise ValueError("Cannot locate artifacts: every package must have an exact version.")
            records.append((requirement, set()))
    if not records or any(not hashes for _, hashes in records):
        raise ValueError("Cannot locate artifacts: every package needs an existing SHA-256 lock hash.")
    parsed_index = urllib.parse.urlsplit(index or "")
    if index is not None and (parsed_index.scheme != "https" or not parsed_index.netloc or parsed_index.username is not None or parsed_index.query):
        raise ValueError("Cannot locate artifacts: supply the lock's HTTPS package index.")
    return records, index.rstrip("/") if index else None, hashlib.sha256(source).hexdigest()


def locate(path, index=None, allowed_sources=(), required_sources=(), direct_sources=(), pool=None):
    records, index, source_sha256 = read_requirements(path, index)
    packages = []
    required_sources = {canonicalize_name(name) for name in required_sources}
    allowed_sources = {canonicalize_name(name) for name in allowed_sources} | required_sources
    direct = {}
    for source in direct_sources:
        parsed = urllib.parse.urlsplit(source)
        filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        distribution, version, _, _ = parse_wheel_filename(filename)
        checksum = urllib.parse.parse_qs(parsed.fragment).get("sha256", [None])[0]
        if parsed.scheme != "https" or parsed.username is not None or parsed.query or not re.fullmatch(r"[0-9a-f]{64}", checksum or ""):
            raise ValueError("Cannot locate artifacts: direct sources require an HTTPS URL with a SHA-256 fragment.")
        if distribution in direct:
            raise ValueError(f"Cannot locate {distribution}: more than one direct source was supplied.")
        direct[distribution] = (source, version, checksum)
    python_version = Version(default_environment()["python_full_version"])
    for requirement, hashes in records:
        name = canonicalize_name(requirement.name)
        supplied = direct.pop(name, None)
        if requirement.url or supplied:
            if requirement.url and supplied:
                raise ValueError(f"Cannot locate {name}: its lock already supplies a direct URL.")
            url = urllib.parse.urlsplit(requirement.url if requirement.url else supplied[0])
            if url.scheme != "https" or url.username is not None or url.query:
                raise ValueError(f"Cannot locate {name}: direct artifact must use HTTPS without credentials.")
            filename = urllib.parse.unquote(url.path.rsplit("/", 1)[-1])
            try:
                distribution, version, _, tags = parse_wheel_filename(filename)
            except InvalidWheelFilename as error:
                raise ValueError(f"Cannot locate {name}: direct artifact is not a named wheel.") from error
            fragment = urllib.parse.parse_qs(url.fragment)
            checksum = fragment.get("sha256", [None])[0]
            if (distribution != name or checksum not in hashes or name in required_sources or not tags.intersection(sys_tags())
                    or (supplied and not requirement.specifier.contains(version, prereleases=True))):
                raise ValueError(f"Cannot locate {name}: direct artifact differs from its locked identity.")
            packages.append({"name": name, "version": str(version), "archive": {
                "url": urllib.parse.urlunsplit(url._replace(fragment="")), "hashes": {"sha256": checksum}}})
            continue
        if index is None:
            raise ValueError(f"Cannot locate {name}: the pinned lock needs an HTTPS package index.")
        version = Version(next(iter(requirement.specifier)).version)
        project_url = f"{index}/{name}/"
        request = urllib.request.Request(project_url, headers={"Accept": "application/vnd.pypi.simple.v1+json"})
        opener = urllib.request.build_opener(HttpsRedirects())
        def read_project():
            with network_request(lambda: opener.open(request, timeout=60)) as response:
                return network_request(response.read)
        try:
            project = json.loads(retry(read_project, f"Cannot locate {name}"))
        except ValueError:
            raise ValueError(f"Cannot locate {name}: index metadata is invalid.") from None
        if canonicalize_name(project["name"]) != name:
            raise ValueError(f"Cannot locate {name}: index returned a different project.")
        wheels, sources = [], []
        for artifact in project.get("files", []):
            filename = artifact.get("filename", "")
            checksum = artifact.get("hashes", {}).get("sha256")
            if checksum not in hashes:
                continue
            requires = artifact.get("requires-python")
            if requires and not SpecifierSet(requires).contains(python_version, prereleases=True):
                continue
            try:
                distribution, artifact_version, _, tags = parse_wheel_filename(filename)
                kind = "wheel"
                if not tags.intersection(sys_tags()):
                    continue
            except InvalidWheelFilename:
                try:
                    distribution, artifact_version = parse_sdist_filename(filename)
                    kind = "source"
                except InvalidSdistFilename:
                    continue
            if distribution != name or not requirement.specifier.contains(artifact_version, prereleases=True):
                continue
            url = urllib.parse.urljoin(project_url, artifact["url"])
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "https" or parsed.username is not None or parsed.query
                    or urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) != filename):
                raise ValueError(f"Cannot locate {name}: index returned an inconsistent artifact URL.")
            item = {"url": url, "hashes": {"sha256": checksum}}
            (wheels if kind == "wheel" else sources).append((filename, item, bool(artifact.get("yanked")), artifact_version))
        wheels.sort(key=lambda item: item[0])
        sources.sort(key=lambda item: (not item[0].endswith(".tar.gz"), item[0]))
        wheels = [item for item in wheels if not item[2]] or wheels
        sources = [item for item in sources if not item[2]] or sources
        if pool is not None and wheels and not (name in allowed_sources and sources):
            # Freeze the reusable choices in this projection so later selection
            # cannot switch back to a different, uncached wheel.
            from wheelhouse_tool.__main__ import inspect_artifact, pool_lock

            with pool_lock(pool, exclusive=False) as directory_fd:
                cached = []
                for item in wheels:
                    try:
                        if inspect_artifact(pool, item[0], item[1]["hashes"]["sha256"], directory_fd):
                            cached.append(item)
                    except ValueError:
                        # A conflicting file is not reusable; leave it in place.
                        continue
            if cached:
                wheels = cached
        candidates = sources if name in allowed_sources and sources else wheels
        if candidates:
            version = max(item[3] for item in candidates)
            wheels = [item for item in wheels if item[3] == version]
            sources = [item for item in sources if item[3] == version]
        package = {"name": name, "version": str(version)}
        if wheels:
            package["wheels"] = [item for _, item, _, _ in wheels]
        if name in allowed_sources and sources:
            package["sdist"] = sources[0][1]
        if name in required_sources and "sdist" not in package:
            raise ValueError(f"Cannot locate {name}: its required source distribution is unavailable.")
        if "wheels" not in package and "sdist" not in package:
            raise ValueError(f"Cannot locate {name}: the index has no artifact admitted by its lock and source policy.")
        packages.append(package)
    if direct:
        raise ValueError("Cannot locate artifacts: a supplied direct source is not in the lock.")
    if required_sources - {package["name"] for package in packages}:
        raise ValueError("Cannot locate artifacts: a required source package is not in the lock.")
    document = {"lock-version": "1.0", "created-by": "nmr-provider-tools metadata projection", "packages": packages,
                "tool": {"nmr-provider-tools": {"source-lock-sha256": source_sha256}}}
    list(Pylock.from_dict(document).select(prefer_sdist_predicate=lambda name: name in allowed_sources))
    return tomli_w.dumps(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--index-url")
    parser.add_argument("--allow-source", action="append", default=[])
    parser.add_argument("--require-source", action="append", default=[])
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--pool", type=Path, help="Prefer compatible, verified wheels already in this pool.")
    args = parser.parse_args()
    try:
        print(locate(args.lock, args.index_url, args.allow_source, args.require_source, args.source, args.pool), end="")
    except (OSError, ValueError, KeyError, TypeError, PylockSelectError, PylockValidationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
