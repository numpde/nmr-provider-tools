"""Admit Docker's projected image identifiers, labels and aliases."""

import re
import reprlib
from collections.abc import Collection
from typing import Any

from operations.docker.adapters.docker_inspection import decode_inspection_lines
from operations.docker.errors import RetirementRefused


# repo-check: allow-module-constant
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")

# repo-check: allow-module-constant
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def inventory_ids(raw: str, pattern: re.Pattern[str]) -> list[str]:
    """Admit full Docker IDs without changing the inventory's order or multiplicity."""

    reported_ids = raw.splitlines()

    for reported_id in reported_ids:
        if pattern.fullmatch(reported_id) is None:
            raise RetirementRefused(f"{reprlib.repr(reported_id)=} should match {pattern.pattern=}")

    return reported_ids


def container_image_references(raw: str, expected_count: int) -> list[str]:
    """Admit one immutable image ID per inspected container."""

    container_images = decode_inspection_lines(raw)

    if len(container_images) != expected_count:
        raise RetirementRefused(f"{len(container_images)=} should match {expected_count=}")

    for container_image_id in container_images:
        if not (type(container_image_id) is (expected_type := str)):
            raise RetirementRefused(f"{type(container_image_id)=} ≠ {expected_type=}")

        if IMAGE_ID.fullmatch(container_image_id) is None:
            raise RetirementRefused(f"{reprlib.repr(container_image_id)=} should match {IMAGE_ID.pattern=}")

    return container_images


def metadata(raw: str, expected: Collection[str]) -> list[dict[str, Any]]:
    """Admit the response only when its objects account for the requested IDs."""

    inspected_images = decode_inspection_lines(raw)

    if len(inspected_images) != len(expected):
        raise RetirementRefused(f"{len(inspected_images)=} should match {len(expected)=}")

    for inspected_image in inspected_images:
        if not (type(inspected_image) is (expected_type := dict)):
            raise RetirementRefused(f"{type(inspected_image)=} ≠ {expected_type=}")

    if (inspected_image_ids := {image.get("Id") for image in inspected_images}) != (expected_image_ids := set(expected)):
        raise RetirementRefused(f"{reprlib.repr(inspected_image_ids)=} should match {reprlib.repr(expected_image_ids)=}")

    return inspected_images


def inspected_image_fields(image: dict[str, Any]) -> tuple[str, dict[str, str], list[str], list[str]]:
    """Validate the fields before the owner consults labels or aliases."""

    image_id = image.get("Id")

    (image_labels, image_tags, image_digests) = (
        image.get("Labels"),
        image.get("RepoTags"),
        image.get("RepoDigests"),
    )

    if not (type(image_id) is (expected_type := str)):
        raise RetirementRefused(f"{type(image_id)=} ≠ {expected_type=}")

    if IMAGE_ID.fullmatch(image_id) is None:
        raise RetirementRefused(f"{reprlib.repr(image_id)=} should match {IMAGE_ID.pattern=}")

    if not ("RepoTags" in image):
        raise RetirementRefused(f"Inspection of {image_id=} should include RepoTags")

    if not ("RepoDigests" in image):
        raise RetirementRefused(f"Inspection of {image_id=} should include RepoDigests")

    if image_labels is not None:

        if not (type(image_labels) is (expected_image_labels_type := dict)):
            raise RetirementRefused(f"{type(image_labels)=} ≠ {expected_image_labels_type=}")

        for (label_name, label_text) in image_labels.items():
            if not (type(label_name) is (expected_type := str)):
                raise RetirementRefused(f"{type(label_name)=} ≠ {expected_type=}")

            if not (type(label_text) is (expected_type := str)):
                raise RetirementRefused(f"{type(label_text)=} ≠ {expected_type=}")

    for image_aliases in (image_tags, image_digests):
        if image_aliases is None:
            continue

        if not (type(image_aliases) is (expected_type := list)):
            raise RetirementRefused(f"{type(image_aliases)=} ≠ {expected_type=}")

        for image_alias in image_aliases:
            if not (type(image_alias) is (expected_type := str)):
                raise RetirementRefused(f"{type(image_alias)=} ≠ {expected_type=}")

    # silent-defaults: allow-contract -- Docker reports absent optional Labels as null. This absence means no labels.
    image_labels = {} if image_labels is None else image_labels

    # silent-defaults: allow-contract -- Docker reports absent optional RepoTags as null. This absence means no aliases.
    image_tags = [] if image_tags is None else image_tags

    # silent-defaults: allow-contract -- Docker reports absent optional RepoDigests as null. This absence means no digest aliases.
    image_digests = [] if image_digests is None else image_digests

    return (image_id, image_labels, image_tags, image_digests)
