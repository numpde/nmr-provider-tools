"""Retire positively owned obsolete outputs from the main checkout only.

Shared source-directory locking follows nmrnet-repro/tools/image_lifecycle.sh.
JSON admission and exact nonforce removal follow magnet-deploy/scripts/retire-images.py.
Docker remains the authority for late container conflicts. Unknown aliases and
all multiple-tag images remain, including old two-tag delivery publications.
Deployment settings do not select images: the current delivery recipe owns that
selection. Only known retained build IID receipts are read from /tmp.
"""

import pathlib
import re
import reprlib
import resource
import subprocess
import sys
import tempfile

if __name__ == "__main__":
    # Load the checkout and its pure type helper under isolated Python
    repository_root = pathlib.Path(__file__).resolve().parents[2]

    sys.path.insert(0, str(repository_root / "packages/type-requirements/src"))
    sys.path.insert(0, str(repository_root))

from operations.docker.adapters.docker_inspection import (
    CONTAINER_IMAGE_FORMAT,
    IMAGE_INSPECTION_FORMAT,
)

from operations.docker.errors import RetirementRefused

from operations.docker.validation.upstream.docker_inspection import (
    CONTAINER_ID,
    IMAGE_ID,
    container_image_references,
    inspected_image_fields,
    inventory_ids,
    metadata,
)


# repo-check: allow-module-constant
_HASH = re.compile(r"[0-9a-f]{64}")

# repo-check: allow-module-constant
_IMAGE_LABEL_PREFIX = "org.nmr-provider-tools."


def capture_command_output(arguments):
    max_capture_bytes = 2097152

    # Private receipts never become public config or daemon diagnostics.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            command_result = subprocess.run(
                arguments,
                stdout=output,
                stderr=error,
                check=False,
                timeout=30,
                preexec_fn=lambda: resource.setrlimit(
                    resource.RLIMIT_FSIZE,
                    (max_capture_bytes, max_capture_bytes),
                ),
            )
        except (OSError, subprocess.TimeoutExpired) as cause:
            raise RetirementRefused(
                f"Image inspection/removal client did not complete: {cause=!s}; retain remaining images",
            ) from cause

        if command_result.returncode:
            error.seek(0)
            client_stderr = error.read(4096).decode("utf-8", errors="replace")

            raise RetirementRefused(
                f"Image inspection/removal failed with {command_result.returncode=}: {client_stderr=}; "
                f"retain remaining images."
            )

        output.seek(0)
        captured_output = output.read(max_capture_bytes)

        if not (len(captured_output) < max_capture_bytes):
            raise RetirementRefused(f"{len(captured_output)=} should be below {max_capture_bytes=} for a complete image inventory")

        return captured_output.decode("utf-8")


# silent-defaults: allow-contract -- The image-ID parser defaults to tagged images; container-mode IDs are a separately selected caller contract.
def ids(raw, *, container=False):
    reported_ids = inventory_ids(raw, CONTAINER_ID if container else IMAGE_ID)

    return sorted(set(reported_ids))


def current_inputs(root):
    # Call the existing input owners rather than copy their fingerprint recipe.
    selected_inputs = capture_command_output(
        [
            "bash",
            "-eu",
            "-o",
            "pipefail",
            "-c",
            'root=$1; source "$root/operations/checker/image_identity.sh"; checker_fingerprint checker; '
            'source "$root/operations/delivery/image_identity.sh"; delivery_recipe_identity; checker_fingerprint bootstrap',
            "bash",
            str(root),
        ]
    )

    identities = selected_inputs.splitlines()
    expected_identity_count = 3

    if not (len(identities) == expected_identity_count):
        raise RetirementRefused(f"{len(identities)=} should match {expected_identity_count=}")

    for identity in identities:
        if not _HASH.fullmatch(identity):
            raise RetirementRefused(f"{reprlib.repr(identity)=} should match {_HASH.pattern=}")

    return tuple(identities)


def retained_image_ids(directory):
    """Read only the known IID receipt in retained root build operations."""
    protected_image_ids = set()

    for receipt in (
        (
            receipt_path
            for name in ("built-image", "built-bootstrap", "built-checker")
            for receipt_path in directory.glob("nmr-provider-tools.*/" + name)
        )
    ):
        if receipt.is_symlink() or receipt.parent.is_symlink():
            continue

        try:
            with receipt.open("rb") as source:
                receipt_bytes = source.read(73)
        except FileNotFoundError:
            # A cooperating owner may already have cleaned its operation.
            continue

        image_id = receipt_bytes.rstrip(b"\n").decode("ascii", errors="replace")

        if (
            IMAGE_ID.fullmatch(image_id) and
            (receipt_bytes in (image_id.encode(), (image_id + "\n").encode()))
        ):
            protected_image_ids.add(image_id)

    return protected_image_ids


def select_images(images, protected, current):
    retirement_candidates = []
    (current_checker, current_recipe, current_bootstrap) = current

    for image in images:
        (image_id, image_labels, image_tags, image_digests) = inspected_image_fields(image)

        if (image_id in protected) or image_digests or (len(image_tags) > 1):
            continue

        if any(
            (
                (tag in {
                    f"nmr-provider-tools/checker:{current_checker}",  # machine-string: Docker image protection compares these exact repository tags
                    f"nmr-provider-tools/checker-bootstrap:{current_bootstrap}",  # machine-string: Docker image protection compares this exact bootstrap tag
                    f"nmr-provider-tools/delivery-recipe:{current_recipe}",  # machine-string: Docker image protection compares this exact delivery recipe tag
                    "nmr-provider-tools/wheelhouse-tool:local",
                })
                for tag in image_tags
            ),
        ):
            continue

        (role, recorded_inputs) = (
            image_labels.get(_IMAGE_LABEL_PREFIX + "role"),
            image_labels.get(_IMAGE_LABEL_PREFIX + "inputs"),
        )

        if role == "delivery":
            recorded_recipe = image_labels.get(_IMAGE_LABEL_PREFIX + "recipe")

            if not (
                (type(recorded_inputs) is str) and
                _HASH.fullmatch(recorded_inputs) and
                (type(recorded_recipe) is str) and
                _HASH.fullmatch(recorded_recipe)
            ):
                # Older outputs without full ownership remain untouched;
                # they do not prevent retirement of other positively owned images.
                continue

            if recorded_recipe == current_recipe:
                continue

            allowed_tags = {
                f"nmr-provider-tools/delivery:{recorded_inputs}",  # machine-string: Image retirement allows only these exact Docker tags for this recorded owner
                f"nmr-provider-tools/delivery-recipe:{recorded_recipe}",  # machine-string: Image retirement compares the recorded recipe's exact Docker tag
            }
        elif role == "wheelhouse-tool":
            allowed_tags = {"nmr-provider-tools/wheelhouse-tool:local"}
        elif (role == "checker-bootstrap") and (type(recorded_inputs) is str) and _HASH.fullmatch(recorded_inputs):
            if recorded_inputs == current_bootstrap:
                continue

            allowed_tags = {
                f"nmr-provider-tools/checker-bootstrap:{recorded_inputs}",  # machine-string: Image retirement compares the recorded bootstrap's exact Docker tag
            }
        elif (role in (None, "checker")) and (type(recorded_inputs) is str) and _HASH.fullmatch(recorded_inputs):
            if recorded_inputs == current_checker:
                continue

            allowed_tags = {
                f"nmr-provider-tools/checker:{recorded_inputs}",  # machine-string: Image retirement compares the recorded checker's exact Docker tag
            }
        else:
            continue

        if any((tag not in allowed_tags) for tag in image_tags):
            continue

        retirement_candidates.append(image_id)

    return retirement_candidates


# silent-defaults: allow-contract -- Staged images are absent as an immutable tuple unless explicitly retained; the fixed /tmp root holds checker operation receipts for current image borrowers.
def retire(root, docker, current, *, staged=(), operations=pathlib.Path("/tmp")):
    selected_input_identities = current()
    protected_images = set(staged) | retained_image_ids(operations)

    container_ids = ids(docker("ps", "--all", "--quiet", "--no-trunc"), container=True)

    for offset in range(0, len(container_ids), 64):
        batch = container_ids[offset:offset + 64]

        container_images = container_image_references(
            docker(
                "inspect",
                "--type",
                "container",
                "--format",
                CONTAINER_IMAGE_FORMAT,
                *batch
            ),
            len(batch),
        )

        protected_images.update(container_images)

    owned_image_ids = set()

    for label_suffix in ("inputs", "role"):
        owned_image_ids.update(
            ids(
                docker(
                    "image",
                    "ls",
                    "--all",
                    "--quiet",
                    "--no-trunc",
                    "--filter",
                    "label=" + _IMAGE_LABEL_PREFIX + label_suffix,
                )
            )
        )

    inspected_images = []
    owned_image_ids = sorted(owned_image_ids)

    for offset in range(0, len(owned_image_ids), 64):
        batch = owned_image_ids[offset:offset + 64]

        inspected_images.extend(
            metadata(docker("image", "inspect", "--format", IMAGE_INSPECTION_FORMAT, *batch), batch)
        )

    retirement_plan = select_images(inspected_images, protected_images, selected_input_identities)
    removed_count = 0

    for image_id in retirement_plan:
        current_input_identities = current()

        if not (current_input_identities == selected_input_identities):
            raise RetirementRefused(f"{current_input_identities=} should match {selected_input_identities=} before retiring {image_id=}")

        protected_images.update(retained_image_ids(operations))

        borrowers = ids(
            docker(
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "ancestor=" + image_id,
            ),
            container=True,
        )

        if borrowers:
            continue

        latest_image = metadata(
            docker("image", "inspect", "--format", IMAGE_INSPECTION_FORMAT, image_id),
            [image_id],
        )

        if select_images(latest_image, protected_images, selected_input_identities) != [image_id]:
            continue

        # Immutable IDs only. Docker refuses late references; never force or
        # remove a retargetable alias, parent image, cache entry, or volume.
        docker("image", "rm", "--no-prune", image_id)

        removed_count += 1

    return removed_count


def main():
    try:
        (root, staged, *engine) = sys.argv[1:]

        if not (engine):
            raise RetirementRefused(f"{engine=} should contain an explicit engine command")

        if staged:
            if not IMAGE_ID.fullmatch(staged):
                raise RetirementRefused(f"{reprlib.repr(staged)=} should match {IMAGE_ID.pattern=}")

        staged_images = ()

        if staged:
            staged_images = (staged,)

        retired_image_count = retire(
            pathlib.Path(root),
            lambda *arguments: capture_command_output([*engine, *arguments]),
            lambda: current_inputs(pathlib.Path(root)),
            staged=staged_images,
        )

        print(
            f"{retired_image_count=}; current selections, "
            f"aliases and container references remain protected."
        )

        return 0
    # silent-defaults: allow-exception-outcome -- An explicit image-retirement refusal is reported and terminates the CLI with nonzero status.
    except RetirementRefused as failure:
        print(
            f"Cannot complete image retirement: {failure=!s}. Earlier removals are not rolled back.",
            file=sys.stderr,
        )

        return 2
    # silent-defaults: allow-exception-outcome -- Malformed private metadata is reported as a nonzero image-retirement failure, never a successful empty result.
    except (OSError, ValueError, TypeError, KeyError):
        print(
            "Cannot complete image retirement: required metadata was malformed or unreadable; "
            "retain remaining images. Earlier removals are not rolled back.",
            file=sys.stderr,
        )

        return 2


if __name__ == "__main__":
    raise SystemExit(main())
