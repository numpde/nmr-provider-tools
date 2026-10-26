# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/tests/repository/_makefile.py
# Original recipe parsing and exact direct-owner inventory semantics retained.
"""Narrow source inspection for literal Make recipes in repository checks."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()


def make_invocation_owners(makefile: str, invocation: str) -> list[str]:
    """Name every direct Make define or recipe containing an invocation."""

    owners: list[str] = []
    owner: str | None = None
    in_define = False
    for line in makefile.splitlines():
        if in_define:
            if line == "endef":
                owner = None
                in_define = False
                continue
        elif line.startswith("override define "):
            owner = f"define {line.removeprefix('override define ')}"
            in_define = True
            continue
        elif line and not line.startswith("\t") and not line.lstrip().startswith("#"):
            rule_targets, separator, _ = line.partition(":")
            # silent-defaults: allow-contract -- A literal rule names its owner; a nonrule clears the previous owner so a later tabbed invocation cannot inherit it; proof=tests/repository/quality/test_makefile.py::MakeSourceInspectionTests.test_invocation_inventory_rejects_uninspected_uses
            owner = f"target {rule_targets}" if separator else None

        if invocation not in line:
            continue
        before_invocation = line.split(invocation, 1)[0]
        if owner is None or "#" in before_invocation or (not in_define and not line.startswith("\t")):
            raise AssertionError(f"Makefile has {invocation!r} outside one direct recipe or define")
        if owner not in owners:
            owners.append(owner)
    return owners


def make_define_body(makefile: str, name: str) -> str:
    """Return one repository-owned multiline Make variable body."""

    lines = makefile.splitlines()
    marker = f"override define {name}"
    marker_indexes = [index for index, line in enumerate(lines) if line == marker]
    if len(marker_indexes) != 1:
        raise AssertionError(f"Makefile override define {name!r} must appear once")

    body_start = marker_indexes[0] + 1
    try:
        body_end = lines.index("endef", body_start)
    except ValueError as exc:
        raise AssertionError(f"Makefile override define {name!r} has no endef") from exc
    return "\n".join(lines[body_start:body_end])


def make_target_body(makefile: str, target: str) -> str:
    """Return the sole plain literal recipe for a target."""

    lines = makefile.splitlines(keepends=True)
    rule_indexes = [index for index, line in enumerate(lines) if _rule_mentions_target(line, target)]
    if not rule_indexes:
        raise AssertionError(f"Makefile target {target!r} is missing")
    if len(rule_indexes) > 1:
        raise AssertionError(f"Makefile target {target!r} has multiple literal rules")
    rule_target, _, remainder = lines[rule_indexes[0]].partition(":")
    if rule_target != target or remainder.startswith(":") or ";" in remainder:
        raise AssertionError(f"Makefile target {target!r} must have one plain literal rule")

    recipe: list[str] = []
    gap: list[str] = []
    for line in lines[rule_indexes[0] + 1 :]:
        if line.startswith("\t"):
            if recipe:
                recipe.extend(gap)
            gap.clear()
            recipe.append(line)
        elif not line.strip() or line.lstrip().startswith("#"):
            gap.append(line)
        else:
            break
    if not recipe:
        raise AssertionError(f"Makefile target {target!r} has no recipe")
    return "".join(recipe)


def _rule_mentions_target(line: str, target: str) -> bool:
    if line.startswith("\t") or line.lstrip().startswith("#"):
        return False
    rule_targets, separator, _ = line.partition(":")
    return bool(separator) and target in rule_targets.split()
