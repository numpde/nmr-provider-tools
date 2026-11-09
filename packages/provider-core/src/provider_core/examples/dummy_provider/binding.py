"""Bind the fake science to the durable contribution interface."""

import hashlib
import json

from provider_core.contribution import (
    AnalysisHooks,
    AssetDescription,
    CallBinding,
    CallDescription,
    ConcurrencyRequirement,
    ConfigurationDescription,
    ImplementationBinding,
    ImplementationDescription,
    InstanceHooks,
    InterruptionSupport,
    IsolationRequirement,
    ProgressEvent,
    ResourceRequirement,
    ReturnedState,
    ScientificRejection,
    ScientificReturn,
    StateOutput,
    StateParameter,
    StateUse,
)

from . import science


def _encode(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value):
    return "sha256:" + hashlib.sha256(_encode(value)).hexdigest()


def _read_payload(payload):
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScientificRejection("invalid_json", "Input must be a JSON object") from error


def _decode_values(instance, analysis, payload, control):
    document = _read_payload(payload)
    if (
        type(document) is not dict
        or set(document) != {"values"}
        or type(document["values"]) is not list
    ):
        raise ScientificRejection(
            "invalid_input",
            "Input must contain only a values array",
        )
    values = tuple(document["values"])
    try:
        science.require_values(values)
    except science.InputRejected as error:
        raise ScientificRejection(error.code, str(error)) from error
    return values


def _decode_empty(instance, analysis, payload, control):
    if _read_payload(payload) != {}:
        raise ScientificRejection(
            "invalid_input",
            "Calculation takes an empty object and its prepared state reference",
        )
    return None


def _prepare(instance, analysis, values, states, control):
    try:
        prepared = science.prepare(analysis, values)
    except science.InputRejected as error:
        raise ScientificRejection(error.code, str(error)) from error
    return ScientificReturn(
        value={"count": len(prepared.values)},
        states=(
            ReturnedState(
                "prepared",
                "dummy.prepared.v1",
                prepared,
                science.dispose_prepared,
            ),
        ),
        dispose_value=None,
    )


def _calculate(instance, analysis, decoded, states, control):
    control.report_progress(ProgressEvent("calculation", 0, 1))
    result = science.calculate(analysis, states["prepared"])
    control.report_progress(ProgressEvent("calculation", 1, 1))
    return ScientificReturn(result, (), None)


def _sum(instance, analysis, values, states, control):
    return ScientificReturn(science.sum_values(values), (), None)


def _cpu_requirement():
    return ResourceRequirement(
        kind="cpu",
        amount=1,
        unit="thread",
        selector="generic",
        exclusive=False,
    )


def weighted_binding(
    settings: science.Settings,
    coefficients: tuple[float, ...],
) -> ImplementationBinding:
    """Bind immutable settings and coefficient assets to the weighted model."""
    science.require_configuration(settings, coefficients)

    configuration = ConfigurationDescription(
        format="dummy.settings.v1",
        digest=_digest({"scale": settings.scale, "offset": settings.offset}),
    )
    coefficient_document = {"coefficients": coefficients}
    coefficient_asset = AssetDescription(
        name="coefficients",
        media_type="application/vnd.nmr-provider-tools.dummy-coefficients+json",
        digest=_digest(coefficient_document),
        size_bytes=len(_encode(coefficient_document)),
    )
    revision = _digest({
        "implementation": "dummy.weighted.v1",
        "configuration": configuration.digest,
        "assets": [coefficient_asset.digest],
    })

    prepare = CallDescription(
        name="prepare",
        input_format="dummy.values.v1",
        result_format="dummy.accepted.v1",
        state_inputs=(),
        state_outputs=(StateOutput("prepared", "dummy.prepared.v1"),),
        resources=(_cpu_requirement(),),
        concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=InterruptionSupport.NONE,
        progress=False,
    )
    calculate = CallDescription(
        name="calculate",
        input_format="dummy.empty.v1",
        result_format="dummy.prediction.v1",
        state_inputs=(
            StateParameter(
                "prepared",
                "dummy.prepared.v1",
                StateUse.CONSUME,
            ),
        ),
        state_outputs=(),
        resources=(_cpu_requirement(),),
        concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
        interruption=InterruptionSupport.NONE,
        progress=True,
    )
    description = ImplementationDescription(
        binding_id="dummy.weighted",
        binding_revision=revision,
        contract_version=1,
        configuration=configuration,
        assets=(coefficient_asset,),
        calls=(prepare, calculate),
        resident_resources=(
            ResourceRequirement(
                kind="host-memory",
                amount=max(1, 8 * len(coefficients)),
                unit="byte",
                selector="python",
                exclusive=False,
            ),
        ),
        isolation=IsolationRequirement.SHARED_PROCESS,
    )
    return ImplementationBinding(
        description=description,
        instance=InstanceHooks(
            lambda: science.create_model(settings, coefficients),
            science.load_model,
            science.dispose_model,
        ),
        analysis=AnalysisHooks(
            science.create_analysis,
            science.initialize_analysis,
            science.dispose_analysis,
        ),
        calls=(
            CallBinding(prepare, _decode_values, _prepare, _encode),
            CallBinding(calculate, _decode_empty, _calculate, _encode),
        ),
    )


def sum_binding() -> ImplementationBinding:
    """A stateless implementation needs no model or analysis lifecycle hooks."""
    configuration = ConfigurationDescription(
        format="dummy.none.v1",
        digest=_digest({}),
    )
    run = CallDescription(
        name="run",
        input_format="dummy.values.v1",
        result_format="dummy.sum.v1",
        state_inputs=(),
        state_outputs=(),
        resources=(_cpu_requirement(),),
        concurrency=ConcurrencyRequirement.PARALLEL,
        interruption=InterruptionSupport.NONE,
        progress=False,
    )
    return ImplementationBinding(
        description=ImplementationDescription(
            binding_id="dummy.sum",
            binding_revision=_digest({"implementation": "dummy.sum.v1"}),
            contract_version=1,
            configuration=configuration,
            assets=(),
            calls=(run,),
            resident_resources=(),
            isolation=IsolationRequirement.SHARED_PROCESS,
        ),
        instance=None,
        analysis=None,
        calls=(CallBinding(run, _decode_values, _sum, _encode),),
    )
