"""Project a pinned requirements closure to URL-bearing artifact metadata."""

import argparse
import hashlib
import re
import reprlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.markers import default_environment
from packaging.pylock import PylockSelectError, PylockValidationError
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import InvalidSdistFilename, InvalidWheelFilename, canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version
from wheelhouse_tool.adapters import simple_json as simple_requests
from wheelhouse_tool.adapters.pylock import project_archive_record, project_index_record, render_projected_lock
from wheelhouse_tool.network import request as network_request, retry
from wheelhouse_tool.validation.upstream import pip_requirements
from wheelhouse_tool.validation.upstream import simple_json as simple_metadata


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        if not ((redirect_scheme := urllib.parse.urlsplit(url).scheme) == (expected_scheme := "https")):
            raise ValueError(f"{reprlib.repr(redirect_scheme)=} should match {expected_scheme=}")

        return super().redirect_request(request, fp, code, message, headers, url)


def read_requirements(path, configured_index=None):
    """Read a flat pinned closure; this does not resolve dependencies."""
    records = []
    index = configured_index
    source = path.read_bytes()

    for line in pip_requirements.requirement_lines(source.decode("utf-8")):
        if (locked_index := pip_requirements.index_url_option(line)) is not None:
            if (index is not None) and (index.rstrip("/") != locked_index.rstrip("/")):
                raise ValueError(f"{reprlib.repr(index)=} should match {reprlib.repr(locked_index)=}")

            index = locked_index
        elif (checksum := pip_requirements.sha256_option(line)) is not None:
            if not records:
                raise ValueError(f"{reprlib.repr(checksum)=} should follow a pinned requirement")

            pip_requirements.require_sha256(checksum)
            records[-1][1].add(checksum)
        else:
            requirement = Requirement(line)

            if requirement.marker is not None:
                raise ValueError(f"{reprlib.repr(requirement.marker)=} should be None")

            if requirement.extras:
                raise ValueError(f"{reprlib.repr(requirement.extras)=} should be empty")

            if requirement.url is None:
                specifiers = list(requirement.specifier)

                if (len(specifiers) != 1) or (specifiers[0].operator != "==") or ("*" in specifiers[0].version):
                    raise ValueError(f"{reprlib.repr(requirement.specifier)=} should pin one exact version")

            records.append((requirement, set()))

    if (not records) or (any((not hashes) for (_, hashes) in records)):
        raise ValueError("Cannot locate artifacts: every package needs an existing SHA-256 lock hash.")

    parsed_index_input = index

    if parsed_index_input is None:
        parsed_index_input = ""

    parsed_index = urllib.parse.urlsplit(parsed_index_input)

    if (index is not None) and ((parsed_index.scheme != "https") or (not parsed_index.netloc) or (parsed_index.username is not None) or (parsed_index.query)):
        raise ValueError("Cannot locate artifacts: supply the lock's HTTPS package index.")

    index_base = None

    if index:
        index_base = index.rstrip("/")

    return (records, index_base, hashlib.sha256(source).hexdigest())


# silent-defaults: allow-contract -- The caller selects an optional HTTPS index and source preferences; absent preferences are immutable empty tuples, not inferred sources.
def locate(path, index=None, allowed_sources=(), required_sources=(), direct_sources=(), pool=None):
    (records, index, source_sha256) = read_requirements(path, index)
    packages = []
    required_sources = {canonicalize_name(name) for name in required_sources}
    allowed_sources = {canonicalize_name(name) for name in allowed_sources} | required_sources
    direct = {}

    for source in direct_sources:
        parsed = urllib.parse.urlsplit(source)
        filename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        (distribution, version, _, _) = parse_wheel_filename(filename)
        values = urllib.parse.parse_qs(parsed.fragment).get("sha256")
        checksum = None

        if values:
            checksum = values[0]

        if (parsed.scheme != "https") or (parsed.username is not None) or (parsed.query) or (checksum is None) or (not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("Cannot locate artifacts: direct sources require an HTTPS URL with a SHA-256 fragment.")

        if distribution in direct:
            raise ValueError(f"Cannot locate {distribution}: more than one direct source was supplied.")

        direct[distribution] = (source, version, checksum)

    python_version = Version(default_environment()["python_full_version"])

    for (requirement, hashes) in records:
        name = canonicalize_name(requirement.name)
        supplied = None

        if name in direct:
            supplied = direct.pop(name)

        if requirement.url or supplied:
            if requirement.url and supplied:
                raise ValueError(f"Cannot locate {name}: its lock already supplies a direct URL.")

            url = urllib.parse.urlsplit(requirement.url if requirement.url else supplied[0])

            if (url.scheme != "https") or (url.username is not None) or (url.query):
                raise ValueError(f"Cannot locate {name}: direct artifact must use HTTPS without credentials.")

            filename = urllib.parse.unquote(url.path.rsplit("/", 1)[-1])

            try:
                (distribution, version, _, tags) = parse_wheel_filename(filename)
            except InvalidWheelFilename as error:
                raise ValueError(f"Cannot locate {name}: direct artifact is not a named wheel.") from error

            fragment = urllib.parse.parse_qs(url.fragment)
            fragment_values = fragment.get("sha256")
            checksum = None

            if fragment_values:
                checksum = fragment_values[0]

            if (
                (distribution != name) or
                (checksum not in hashes) or
                (name in required_sources) or
                (not tags.intersection(sys_tags())) or
                ((supplied) and (not requirement.specifier.contains(version, prereleases=True)))
            ):
                raise ValueError(f"Cannot locate {name}: direct artifact differs from its locked identity.")

            packages.append(
                project_archive_record(name, version, urllib.parse.urlunsplit(url._replace(fragment="")), checksum),
            )

            continue

        if index is None:
            raise ValueError(f"Cannot locate {name}: the pinned lock needs an HTTPS package index.")

        version = Version(next(iter(requirement.specifier)).version)
        request = simple_requests.project_request(index, name)
        project_url = request.full_url
        opener = urllib.request.build_opener(HttpsRedirects())

        def read_project():
            with network_request(lambda: opener.open(request, timeout=60)) as response:
                return network_request(response.read)

        files = simple_metadata.project_files(retry(read_project, f"Cannot locate {name}"), expected_project_name=name)

        (wheels, sources) = ([], [])

        for artifact in files:
            identity = simple_metadata.file_identity(artifact)

            if identity is None:
                continue

            (filename, checksum) = identity

            if checksum not in hashes:
                continue

            if not simple_metadata.supports_python(artifact, python_version):
                continue

            try:
                (distribution, artifact_version, _, tags) = parse_wheel_filename(filename)
                kind = "wheel"

                if not tags.intersection(sys_tags()):
                    continue
            except InvalidWheelFilename:
                try:
                    (distribution, artifact_version) = parse_sdist_filename(filename)
                    kind = "source"
                except InvalidSdistFilename:
                    continue

            if (distribution != name) or (not requirement.specifier.contains(artifact_version, prereleases=True)):
                continue

            url = simple_requests.file_url(project_url, artifact)
            parsed = urllib.parse.urlsplit(url)

            if not (
                (parsed.scheme == "https") and
                (parsed.username is None) and
                (not parsed.query) and
                (urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) == filename)
            ):
                raise ValueError(f"{reprlib.repr(url)=} should use HTTPS and name {reprlib.repr(filename)=} without credentials or a query")

            (wheels if kind == "wheel" else sources).append(
                (filename, (url, checksum), simple_metadata.file_yanked(artifact), artifact_version),
            )

        wheels.sort(key=lambda item: item[0])
        sources.sort(key=lambda item: (not item[0].endswith(".tar.gz"), item[0]))
        wheels = [item for item in wheels if not item[2]] or wheels
        sources = [item for item in sources if not item[2]] or sources

        if (pool is not None) and (wheels) and (not ((name in allowed_sources) and sources)):
            # Freeze the reusable choices in this projection so later selection
            # cannot switch back to a different, uncached wheel.
            from wheelhouse_tool.__main__ import inspect_artifact, pool_lock

            with pool_lock(pool, exclusive=False) as directory_fd:
                cached = []

                for item in wheels:
                    (filename, (_, checksum), _, _) = item

                    try:
                        if inspect_artifact(pool, filename, checksum, directory_fd):
                            cached.append(item)
                    except ValueError:
                        # A conflicting file is not reusable; leave it in place.
                        continue

            if cached:
                wheels = cached

        candidates = sources if (name in allowed_sources) and sources else wheels

        if candidates:
            version = max(item[3] for item in candidates)
            wheels = [item for item in wheels if item[3] == version]
            sources = [item for item in sources if item[3] == version]

        selected_source = sources[0][1] if (name in allowed_sources) and sources else None

        if (name in required_sources) and (selected_source is None):
            raise ValueError(f"Cannot locate {name}: its required source distribution is unavailable.")

        if (not wheels) and (selected_source is None):
            raise ValueError(f"Cannot locate {name}: the index has no artifact admitted by its lock and source policy.")

        packages.append(
            project_index_record(
                name,
                version,
                wheels=(selection for (_, selection, _, _) in wheels),
                sdist=selected_source,
            ),
        )

    if direct:
        raise ValueError("Cannot locate artifacts: a supplied direct source is not in the lock.")

    if required_sources - {canonicalize_name(requirement.name) for (requirement, _) in records}:
        raise ValueError("Cannot locate artifacts: a required source package is not in the lock.")

    return render_projected_lock(
        packages,
        created_by="nmr-provider-tools metadata projection",
        tool={"nmr-provider-tools": {"source-lock-sha256": source_sha256}},
        prefer_sdist_predicate=lambda name: name in allowed_sources,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--index-url")
    parser.add_argument("--allow-source", action="append")
    parser.add_argument("--require-source", action="append")
    parser.add_argument("--source", action="append")
    parser.add_argument("--pool", type=Path, help="Prefer compatible, verified wheels already in this pool.")
    args = parser.parse_args()
    allowed = []
    required = []
    sources = []

    if args.allow_source is not None:
        allowed = args.allow_source

    if args.require_source is not None:
        required = args.require_source

    if args.source is not None:
        sources = args.source

    try:
        print(locate(args.lock, args.index_url, allowed, required, sources, args.pool), end="")
    # silent-defaults: allow-exception-outcome -- Invalid metadata or an unreachable selected index produces a nonzero source-projection CLI result.
    except (OSError, ValueError, KeyError, TypeError, PylockSelectError, PylockValidationError) as error:
        print(str(error), file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
