"""A source-grounded G01 adoption fixture, never a scientific implementation.

Authority: magnet-deploy c5813ee35cfbb1c98a5cc5d20a94787271f26486,
magnet_provider/analysis_contract.py:77-94,145-206 and models/g_01_nmr_forward/
provider/magnet_provider_bindings/g_01_nmr_forward/processor.py:182-283.
The current processor requires an actual future split at interpretation/prediction;
this fixture exercises that proposed seam without importing or migrating Magnet.

Model-input names follow models/g_01_nmr_forward/magnet_runner_contracts/
runner_g_01_nmr_forward/session.py::encode_model_input. Synthetic decimal-string
rows follow the example layout in tests/unit/
test_g01_provider_processor.py::_response; values here are authored sentinels,
not predictions or chemistry. The future field tests that core treats product
bytes as opaque. The fixture supplies admitted CDCl3 and does not reimplement
the release's solvent vocabulary, interpreter, atom projection or result validator.
Retirement acknowledges these scripted async ports only, never a real runner exit.
"""

import json
from hashlib import sha256

from provider_core.analysis import AnalysisSuccess, PreparedAnalysis
from provider_core.attempt_record import ProtectedEvidence

RESULT_SCHEMA_ID = "magnet.g01.prediction_observation.v1"
ANALYSIS_KIND = "g01_local_fixture"
SOURCE = b'Predict both heads for the exact SMILES "  CCO  " in CDCl3.'


class RetrySafeUnavailable(RuntimeError):
    """Binding certifies this happened before a nonrepeatable prediction effect."""


class IndeterminatePrediction(RuntimeError):
    """Binding cannot establish the outcome of its single prediction invocation."""


def prediction():
    decimal = "2.718281828459045235360287"
    return {
        "projected_atom_mapped_cxsmiles": "synthetic-projection-only",
        "h_atom_indices": [4],
        "h_shifts_ppm": [decimal],
        "h_reported_j_candidates_hz": [[decimal] * 8],
        "h_reported_j_count_probabilities": [[decimal] * 9],
        "h_multiplicity": ["s"],
        "c_atom_indices": [2],
        "c_shifts_ppm": ["42.000000000001"],
        "future_binding_field": {"retain": "exact product-owned content"},
    }


class MagnetSession:
    """Await interpretation before issuing a handle; predict only after entry."""

    def __init__(self, store, interpreter, worker):
        self.store, self.interpreter, self.worker = store, interpreter, worker
        self.phases = []
        self.retired = False
        self._prepared = None

    # Match the required session port; this scientific-shape fixture emits no conditions.
    async def prepare(self, acknowledged, instructions, *, report_condition):  # noqa: ARG002
        """The scripted interpreter returns an already admitted candidate.

        Real interpretation admission belongs to Magnet's capability; malformed
        candidate rejection is outside these three outcome-seam cases.
        """
        if acknowledged.start.input != instructions.identity or self.retired or self._prepared is not None:
            raise ValueError("Magnet fixture preparation requires its exact live admitted input")
        self.phases.append(type((await self.store.inventory())[0]))
        value = await self.interpreter(instructions.specification_bytes)
        model_input = {"smiles": value["smiles"], "h_solvent": value["solvent"], "c_solvent": value["solvent"]}
        candidate = json.dumps(model_input, sort_keys=True, separators=(",", ":")).encode()
        handle = PreparedAnalysis(ProtectedEvidence("fixture.magnet.input.v1", sha256(candidate).hexdigest().encode("ascii")))
        key = json.loads(acknowledged.start.body)["provider_attempt_key"]
        self._prepared = handle, key, model_input
        return handle

    # Match the required session port; this scientific-shape fixture emits no conditions.
    async def execute(self, handle, *, report_condition):  # noqa: ARG002
        if self.retired or self._prepared is None or self._prepared[0] is not handle:
            raise ValueError("Magnet fixture execution requires its current unused prepared handle")
        _, key, model_input = self._prepared
        self._prepared = None
        self.phases.append(type((await self.store.inventory())[0]))
        result = await self.worker(model_input)
        return AnalysisSuccess(
            RESULT_SCHEMA_ID,
            self._result(key, model_input, result),
            ProtectedEvidence("fixture.magnet.evidence.v1", b"scripted async product result"),
        )

    async def retire(self):
        self._prepared = None
        self.retired = True

    def _result(self, key, model_input, result):
        return json.dumps(
            {"schema_id": RESULT_SCHEMA_ID, "provider_attempt_key": key, "model_input": model_input, "prediction": result},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
