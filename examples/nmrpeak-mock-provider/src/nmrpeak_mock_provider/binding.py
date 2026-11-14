"""Host the two NMRPeak input contracts on the same scientific implementation."""

from collections.abc import Callable
from pathlib import Path

import provider_core.scientific as sdk

from . import science
from .checkpoint import open_checkpoint


class PeakContribution(sdk.Contribution):
    prepared_kind = sdk.StateKind(name="nmrpeak.validated-input.v1", value_type=science.PreparedInput)
    prepared_output = prepared_kind.output(name="validated", dispose=science.dispose_prepared)
    prepared_input = prepared_kind.input(name="candidate", use=sdk.StateUse.CONSUME)

    def __init__(
        self, *, configuration: science.Settings, checkpoint_path: Path,
        create_model: Callable[[], science.Model],
    ) -> None:
        super().__init__()
        self.configuration = configuration
        # The deployment path locates the configured content; it is not its identity.
        # Capturing a path/factory opens no file and loads no scientific resource.
        self.checkpoint_path = checkpoint_path
        self.create_model = create_model
        self.model: science.Model | None = None

    def initialize(self, *, assets: sdk.AssetAccess) -> None:
        self.model = self.create_model()
        with open_checkpoint(
            path=self.checkpoint_path, expected_sha256=self.configuration.checkpoint_sha256,
        ) as checkpoint:
            self.model.load(checkpoint=checkpoint)

    def close(self) -> None:
        if self.model is not None:
            self.model.close()

    def prepare_input(self, *, candidate: science.HfInput | science.ChfInput) -> sdk.ScientificReturn[science.Validation]:
        prepared = science.prepare(model=self.model, candidate=candidate, settings=self.configuration)
        return sdk.ScientificReturn(
            value=science.Validation(token_count=len(prepared.tokens)),
            states=(self.prepared_output.returned(value=prepared),),
        )

    @sdk.operation(state_inputs=(prepared_input,))
    def generate(self, *, candidate: science.PreparedInput) -> science.Candidates:
        tokens = candidate.consume()
        generated = self.model.generate(tokens=tokens, beam_size=self.configuration.beam_size)
        science.validate_candidates(candidates=generated, settings=self.configuration)
        return science.Candidates(
            generated_smiles=generated,
            checkpoint_sha256=self.configuration.checkpoint_sha256,
        )


class Hf(PeakContribution):
    @sdk.operation(state_outputs=(PeakContribution.prepared_output,))
    def prepare(self, *, candidate: science.HfInput) -> sdk.ScientificReturn[science.Validation]:
        return self.prepare_input(candidate=candidate)


class Chf(PeakContribution):
    @sdk.operation(state_outputs=(PeakContribution.prepared_output,))
    def prepare(self, *, candidate: science.ChfInput) -> sdk.ScientificReturn[science.Validation]:
        return self.prepare_input(candidate=candidate)
