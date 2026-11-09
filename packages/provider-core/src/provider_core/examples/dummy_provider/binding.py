"""Bind the fake science to the shared interface; keep configuration here."""

import hashlib
import json

from provider_core.contribution import (
    AnalysisHooks, CallBinding, CallDescription, ImplementationBinding,
    ImplementationDescription, InstanceHooks, ReturnedState, ScientificRejection,
    ScientificReturn, StateOutput, StateParameter, StateUse,
)

from . import science


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read_payload(payload):
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScientificRejection("invalid_json", "Input must be a JSON object") from error


def _decode_values(instance, analysis, payload):
    document = _read_payload(payload)
    if type(document) is not dict or set(document) != {"values"} or type(document["values"]) is not list:
        raise ScientificRejection("invalid_input", "Input must contain only a values array")
    # Freeze the scientific values before retaining them across calls.
    values = tuple(document["values"])
    try:
        science.require_values(values)
    except science.InputRejected as error:
        raise ScientificRejection(error.code, str(error)) from error
    return values


def _decode_empty(instance, analysis, payload):
    if _read_payload(payload) != {}:
        raise ScientificRejection("invalid_input", "Calculation takes an empty object and its prepared state reference")
    return None


def _prepare(instance, analysis, values, states):
    try:
        prepared = science.prepare(analysis, values)
    except science.InputRejected as error:
        raise ScientificRejection(error.code, str(error)) from error
    return ScientificReturn(
        value={"count": len(prepared.values)},
        states=(ReturnedState("prepared", "dummy.prepared.v1", prepared, science.dispose_prepared),),
        dispose_value=None,
    )


def _calculate(instance, analysis, decoded, states):
    return ScientificReturn(science.calculate(analysis, states["prepared"]), (), None)


def _sum(instance, analysis, values, states):
    return ScientificReturn(science.sum_values(values), (), None)


def weighted_binding(settings: science.Settings, coefficients: tuple[float, ...]) -> ImplementationBinding:
    """The revision fixes both settings and the small coefficient asset."""
    science.require_configuration(settings, coefficients)
    # Bump the declared implementation version when numerical behavior changes.
    revision = hashlib.sha256(_encode({
        "implementation": "dummy.weighted.v1", "scale": settings.scale,
        "offset": settings.offset, "coefficients": coefficients,
    })).hexdigest()
    prepare = CallDescription(
        "prepare", "dummy.values.v1", "dummy.accepted.v1", (),
        (StateOutput("prepared", "dummy.prepared.v1"),), "dummy.call",
    )
    calculate = CallDescription(
        "calculate", "dummy.empty.v1", "dummy.prediction.v1",
        (StateParameter("prepared", "dummy.prepared.v1", StateUse.CONSUME),), (), "dummy.call",
    )
    description = ImplementationDescription("dummy.weighted", revision, 1, (prepare, calculate), "dummy.instance")
    return ImplementationBinding(
        description=description,
        instance=InstanceHooks(lambda: science.create_model(settings, coefficients), science.load_model, science.dispose_model),
        analysis=AnalysisHooks(science.create_analysis, science.initialize_analysis, science.dispose_analysis),
        calls=(CallBinding(prepare, _decode_values, _prepare, _encode), CallBinding(calculate, _decode_empty, _calculate, _encode)),
    )


def sum_binding() -> ImplementationBinding:
    """A stateless implementation needs no model or analysis lifecycle hooks."""
    run = CallDescription("run", "dummy.values.v1", "dummy.sum.v1", (), (), "dummy.call")
    return ImplementationBinding(
        description=ImplementationDescription("dummy.sum", "v1", 1, (run,), "dummy.instance"),
        instance=None,
        analysis=None,
        calls=(CallBinding(run, _decode_values, _sum, _encode),),
    )
