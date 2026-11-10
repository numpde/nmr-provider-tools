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


# silent-defaults: allow-contract -- The image-ID parser defaults to tagged images; container-mode IDs are a separately selected caller contract.
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
    retirement_candidates = []
    (current_checker, current_recipe) = current
    for image in images:
        image_id = image.get("Id")
        (image_labels, image_tags, image_digests) = (
            image.get("Labels"),
            image.get("RepoTags"),
            image.get("RepoDigests"),
        )
        if (
            type(image_id) is not str
            or _ID.fullmatch(image_id) is None
            or "RepoTags" not in image
            or "RepoDigests" not in image
            or image_labels is not None and (
                type(image_labels) is not dict
                or any(
                    type(key) is not str or type(value) is not str
                    for (key, value) in image_labels.items()
                )
            )
            or any(
                value is not None and (
                    type(value) is not list
                    or any(type(item) is not str for item in value)
                )
                for value in (image_tags, image_digests)
            )
        ):
            raise RetirementRefused("Docker returned malformed image ownership or aliases.")

        # silent-defaults: allow-contract -- Docker reports absent optional Labels as null; only that absence means no labels.
        image_labels = {} if image_labels is None else image_labels
        # silent-defaults: allow-contract -- Docker reports absent optional RepoTags as null; only that absence means no aliases.
        image_tags = [] if image_tags is None else image_tags
        # silent-defaults: allow-contract -- Docker reports absent optional RepoDigests as null; only that absence means no digest aliases.
        image_digests = [] if image_digests is None else image_digests

        if image_id in protected or image_digests or len(image_tags) > 1:
            continue
        if any(
            tag in {
                f"nmr-provider-tools/checker:{current_checker}",
                f"nmr-provider-tools/delivery-recipe:{current_recipe}",
                "nmr-provider-tools/wheelhouse-tool:local",
            }
            for tag in image_tags
        ):
            continue

        (role, recorded_inputs) = (
            image_labels.get(_LABEL + "role"),
            image_labels.get(_LABEL + "inputs"),
        )
        if role == "delivery":
            recorded_recipe = image_labels.get(_LABEL + "recipe")
            if not (
                type(recorded_inputs) is str
                and _HASH.fullmatch(recorded_inputs)
                and type(recorded_recipe) is str
                and _HASH.fullmatch(recorded_recipe)
            ):
                # Older outputs without full ownership remain untouched; they
                # do not prevent retirement of other positively owned images.
                continue
            if recorded_recipe == current_recipe:
                continue
            allowed_tags = {
                f"nmr-provider-tools/delivery:{recorded_inputs}",
                f"nmr-provider-tools/delivery-recipe:{recorded_recipe}",
            }
        elif role == "wheelhouse-tool":
            allowed_tags = {"nmr-provider-tools/wheelhouse-tool:local"}
        elif role is None and type(recorded_inputs) is str and _HASH.fullmatch(recorded_inputs):
            if recorded_inputs == current_checker:
                continue
            allowed_tags = {f"nmr-provider-tools/checker:{recorded_inputs}"}
        else:
            continue

        if any(tag not in allowed_tags for tag in image_tags):
            continue

        created_at = image.get("Created")
        if type(created_at) is not str:
            raise RetirementRefused("An owned image has no creation time.")

        created_instant = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created_instant.utcoffset() is None:
            raise RetirementRefused("An owned image creation time has no timezone.")

        created_timestamp = created_instant.timestamp()
        if now - created_timestamp >= 86400:
            retirement_candidates.append(image_id)

    return retirement_candidates


# silent-defaults: allow-contract -- Staged images are absent as an immutable tuple unless explicitly retained; the fixed /tmp root holds checker operation receipts for current image borrowers.
def retire(root, docker, current, *, staged=(), now=None, operations=Path("/tmp")):
    now = time.time() if now is None else now
    selected_inputs = current()
    protected_images = set(staged) | retained_image_ids(operations)

    container_ids = ids(docker("ps", "--all", "--quiet", "--no-trunc"), container=True)
    for offset in range(0, len(container_ids), 64):
        batch = container_ids[offset:offset + 64]
        container_images = [
            json.loads(line)
            for line in docker(
                "inspect", "--type", "container", "--format", "{{json .Image}}", *batch
            ).splitlines()
        ]
        if len(container_images) != len(batch) or any(
            type(item) is not str or _ID.fullmatch(item) is None
            for item in container_images
        ):
            raise RetirementRefused("Container image references were incomplete.")
        protected_images.update(container_images)

    owned_image_ids = set()
    for label in ("inputs", "role"):
        owned_image_ids.update(
            ids(
                docker(
                    "image", "ls", "--all", "--quiet", "--no-trunc",
                    "--filter", "label=" + _LABEL + label,
                )
            )
        )

    inspected_images = []
    owned_image_ids = sorted(owned_image_ids)
    for offset in range(0, len(owned_image_ids), 64):
        batch = owned_image_ids[offset:offset + 64]
        inspected_images.extend(
            metadata(docker("image", "inspect", "--format", _IMAGE_JSON, *batch), batch)
        )

    retirement_plan = select_images(inspected_images, protected_images, selected_inputs, now)
    removed_count = 0
    for image_id in retirement_plan:
        if current() != selected_inputs:
            raise RetirementRefused(
                "Current source selection changed; retain the remaining images and retry."
            )

        protected_images.update(retained_image_ids(operations))
        borrowers = ids(
            docker(
                "ps", "--all", "--quiet", "--no-trunc",
                "--filter", "ancestor=" + image_id,
            ),
            container=True,
        )
        if borrowers:
            continue

        latest_image = metadata(
            docker("image", "inspect", "--format", _IMAGE_JSON, image_id),
            [image_id],
        )
        if select_images(latest_image, protected_images, selected_inputs, now) != [image_id]:
            continue

        # Immutable IDs only. Docker refuses late references; never force or
        # remove a retargetable alias, parent image, cache entry, or volume.
        docker("image", "rm", "--no-prune", image_id)
        removed_count += 1
    return removed_count


def main():
    try:
        root, staged, *engine = sys.argv[1:]
        if not engine or staged and _ID.fullmatch(staged) is None:
            raise RetirementRefused("No explicit engine or immutable staged image was supplied.")
        staged_images = ()
        if staged:
            staged_images = (staged,)
        removed = retire(Path(root), lambda *arguments: run([*engine, *arguments]),
                         lambda: current_inputs(Path(root)), staged=staged_images)
        print(f"Retired {removed} obsolete project images; current selections, aliases and container references remain protected.")
        return 0
    # silent-defaults: allow-exception-outcome -- An explicit image-retirement refusal is reported and terminates the CLI with nonzero status.
    except RetirementRefused as failure:
        print(f"Cannot complete image retirement: {failure} Earlier removals are not rolled back.", file=sys.stderr)
        return 2
    # silent-defaults: allow-exception-outcome -- Malformed private metadata is reported as a nonzero image-retirement failure, never a successful empty result.
    except (OSError, ValueError, TypeError, KeyError):
        print("Cannot complete image retirement: required metadata was malformed or unreadable; retain remaining images. Earlier removals are not rolled back.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
