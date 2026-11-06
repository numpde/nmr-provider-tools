"""Retire positively owned obsolete outputs from the main checkout only.

Shared source-directory locking follows nmrnet-repro/tools/image_lifecycle.sh.
JSON admission and exact nonforce removal follow magnet-deploy/scripts/retire-images.py.
Docker remains the authority for late container conflicts. Unknown aliases and
all multiple-tag images remain, including old two-tag delivery publications.
Deployment settings do not select images: the current delivery recipe owns that
selection. Only known retained build IID receipts are read from /tmp.
"""

from datetime import datetime
import json
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import time


class RetirementRefused(RuntimeError):
    pass


# repo-check: allow-module-constant
_ID = re.compile(r"sha256:[0-9a-f]{64}")
# repo-check: allow-module-constant
_HASH = re.compile(r"[0-9a-f]{64}")
# repo-check: allow-module-constant
_LABEL = "org.nmr-provider-tools."
# repo-check: allow-module-constant
_IMAGE_JSON = ('{"Id":{{json .Id}},"Labels":{{json .Config.Labels}},'
               '"RepoTags":{{json .RepoTags}},"RepoDigests":{{json .RepoDigests}},'
               '"Created":{{json .Created}}}')


def run(arguments):
    # Private receipts never become public config or daemon diagnostics.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            result = subprocess.run(arguments, stdout=output, stderr=error, check=False, timeout=30,
                                    preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152)))
        except (OSError, subprocess.TimeoutExpired):
            raise RetirementRefused("An image inspection/removal client did not complete; retain remaining images.") from None
        if result.returncode:
            raise RetirementRefused(f"An image inspection/removal command failed (status {result.returncode}); retain remaining images.")
        output.seek(0)
        raw = output.read(2097152)
        if len(raw) >= 2097152:
            raise RetirementRefused("Image metadata reached its capture limit; no complete inventory was established.")
        return raw.decode("utf-8")


def ids(raw, *, container=False):
    values = raw.splitlines()
    pattern = re.compile(r"[0-9a-f]{64}") if container else _ID
    if any(pattern.fullmatch(value) is None for value in values):
        raise RetirementRefused("Docker returned an incomplete immutable-ID inventory.")
    return sorted(set(values))


def current_inputs(root):
    # Call the existing input owners rather than copy their fingerprint recipe.
    raw = run(["bash", "-eu", "-o", "pipefail", "-c",
               'root=$1; source "$root/tools/checker_inputs.sh"; checker_fingerprint; '
               'source "$root/tools/delivery_identity.sh"; delivery_recipe_identity', "bash", str(root)])
    values = raw.splitlines()
    if len(values) != 2 or any(_HASH.fullmatch(value) is None for value in values):
        raise RetirementRefused("Current checker/delivery identities are incomplete.")
    return tuple(values)


def retained_image_ids(directory):
    """Read only the known IID receipt in retained root build operations."""
    protected = set()
    for receipt in directory.glob("nmr-provider-tools.*/built-image"):
        if receipt.is_symlink() or receipt.parent.is_symlink():
            continue
        try:
            with receipt.open("rb") as source:
                raw = source.read(73)
        except FileNotFoundError:
            # A cooperating owner may already have cleaned its operation.
            continue
        identifier = raw.rstrip(b"\n").decode("ascii", errors="replace")
        if _ID.fullmatch(identifier) and raw in (identifier.encode(), (identifier + "\n").encode()):
            protected.add(identifier)
    return protected


def metadata(raw, expected):
    values = [json.loads(line) for line in raw.splitlines()]
    if (len(values) != len(expected) or any(type(value) is not dict for value in values)
            or {value.get("Id") for value in values} != set(expected)):
        raise RetirementRefused("Docker image inspection was incomplete.")
    return values


def select_images(images, protected, current, now):
    selected = []
    checker, recipe = current
    for image in images:
        identifier = image.get("Id")
        labels, tags, digests = image.get("Labels"), image.get("RepoTags"), image.get("RepoDigests")
        if (type(identifier) is not str or _ID.fullmatch(identifier) is None
                or "RepoTags" not in image or "RepoDigests" not in image
                or labels is not None and (type(labels) is not dict
                    or any(type(k) is not str or type(v) is not str for k, v in labels.items()))
                or any(value is not None and (type(value) is not list
                    or any(type(item) is not str for item in value)) for value in (tags, digests))):
            raise RetirementRefused("Docker returned malformed image ownership or aliases.")
        # silent-defaults: allow-contract -- Docker reports absent optional Labels as null; only that absence means no labels.
        labels = {} if labels is None else labels
        # silent-defaults: allow-contract -- Docker reports absent optional RepoTags as null; only that absence means no aliases.
        tags = [] if tags is None else tags
        # silent-defaults: allow-contract -- Docker reports absent optional RepoDigests as null; only that absence means no digest aliases.
        digests = [] if digests is None else digests
        if identifier in protected or digests or len(tags) > 1:
            continue
        if any(tag in {f"nmr-provider-tools/checker:{checker}",
                       f"nmr-provider-tools/delivery-recipe:{recipe}",
                       "nmr-provider-tools/wheelhouse-tool:local"} for tag in tags):
            continue
        role, inputs = labels.get(_LABEL + "role"), labels.get(_LABEL + "inputs")
        if role == "delivery":
            recorded_recipe = labels.get(_LABEL + "recipe")
            if not (type(inputs) is str and _HASH.fullmatch(inputs)
                    and type(recorded_recipe) is str and _HASH.fullmatch(recorded_recipe)):
                # Older outputs without full ownership remain untouched; they
                # do not prevent retirement of other positively owned images.
                continue
            if recorded_recipe == recipe:
                continue
            allowed = {f"nmr-provider-tools/delivery:{inputs}",
                       f"nmr-provider-tools/delivery-recipe:{recorded_recipe}"}
        elif role == "wheelhouse-tool":
            allowed = {"nmr-provider-tools/wheelhouse-tool:local"}
        elif role is None and type(inputs) is str and _HASH.fullmatch(inputs):
            if inputs == checker:
                continue
            allowed = {f"nmr-provider-tools/checker:{inputs}"}
        else:
            continue
        if any(tag not in allowed for tag in tags):
            continue
        created = image.get("Created")
        if type(created) is not str:
            raise RetirementRefused("An owned image has no creation time.")
        instant = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise RetirementRefused("An owned image creation time has no timezone.")
        timestamp = instant.timestamp()
        if now - timestamp >= 86400:
            selected.append(identifier)
    return selected


def retire(root, docker, current, *, staged=(), now=None, operations=Path("/tmp")):
    now = time.time() if now is None else now
    selected_inputs = current()
    protected = set(staged) | retained_image_ids(operations)
    containers = ids(docker("ps", "--all", "--quiet", "--no-trunc"), container=True)
    for offset in range(0, len(containers), 64):
        batch = containers[offset:offset + 64]
        images = [json.loads(line) for line in docker(
            "inspect", "--type", "container", "--format", "{{json .Image}}", *batch).splitlines()]
        if len(images) != len(batch) or any(type(item) is not str or _ID.fullmatch(item) is None for item in images):
            raise RetirementRefused("Container image references were incomplete.")
        protected.update(images)
    owned = set()
    for label in ("inputs", "role"):
        owned.update(ids(docker("image", "ls", "--all", "--quiet", "--no-trunc", "--filter", "label=" + _LABEL + label)))
    images = []
    owned = sorted(owned)
    for offset in range(0, len(owned), 64):
        batch = owned[offset:offset + 64]
        images.extend(metadata(docker("image", "inspect", "--format", _IMAGE_JSON, *batch), batch))
    plan = select_images(images, protected, selected_inputs, now)
    removed = 0
    for identifier in plan:
        if current() != selected_inputs:
            raise RetirementRefused("Current source selection changed; retain the remaining images and retry.")
        protected.update(retained_image_ids(operations))
        borrowers = ids(docker("ps", "--all", "--quiet", "--no-trunc", "--filter", "ancestor=" + identifier), container=True)
        if borrowers:
            continue
        latest = metadata(docker("image", "inspect", "--format", _IMAGE_JSON, identifier), [identifier])
        if select_images(latest, protected, selected_inputs, now) != [identifier]:
            continue
        # Immutable IDs only. Docker refuses late references; never force or
        # remove a retargetable alias, parent image, cache entry, or volume.
        docker("image", "rm", "--no-prune", identifier)
        removed += 1
    return removed


def main():
    try:
        root, staged, *engine = sys.argv[1:]
        if not engine or staged and _ID.fullmatch(staged) is None:
            raise RetirementRefused("No explicit engine or immutable staged image was supplied.")
        removed = retire(Path(root), lambda *arguments: run([*engine, *arguments]),
                         lambda: current_inputs(Path(root)), staged=(staged,) if staged else ())
        print(f"Retired {removed} obsolete project images; current selections, aliases and container references remain protected.")
        return 0
    except RetirementRefused as failure:
        print(f"Cannot complete image retirement: {failure} Earlier removals are not rolled back.", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("Cannot complete image retirement: required metadata was malformed or unreadable; retain remaining images. Earlier removals are not rolled back.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
