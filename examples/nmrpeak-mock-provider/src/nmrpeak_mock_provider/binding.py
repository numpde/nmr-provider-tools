"""Expose HF/CHF prepare and generate methods for the SDK to execute.

Each object retains its model and any partially loaded components so SDK-requested
cleanup can close them even when initialization fails.
"""

from collections.abc import Callable
from pathlib import Path

import provider_core.scientific as sdk
from provider_core.verified_file import open_verified_file

from . import science


class PeakContribution(sdk.Contribution):
    """Own a model and the prepared-input port shared by HF and CHF."""

    prepared_state_type = sdk.StateType(value_type=science.PreparedInput)
    validated = prepared_state_type.output(dispose=science.dispose_prepared)
    candidate = prepared_state_type.input(use=sdk.StateUse.CONSUME)

    def __init__(
        self,
        *,
        configuration: science.Settings,
        checkpoint_path: Path,
        create_model: Callable[[], science.Model],
    ) -> None:
        """Retain inputs without opening the checkpoint or creating the model."""
        super().__init__()
        self.configuration = configuration

        # Assembly supplies the file location; configuration supplies its expected digest.
        self.checkpoint_path = checkpoint_path

        self.create_model = create_model
        self.model: science.Model | None = None

    def initialize(self, *, assets: sdk.AssetAccess) -> None:
        """SDK calls this before science; retain the model before loading can fail."""
        self.model = self.create_model()

        with open_verified_file(
            source_file_path=self.checkpoint_path,
            expected_sha256=self.configuration.checkpoint_sha256,
        ) as checkpoint:
            self.model.load(checkpoint=checkpoint)

    def close(self) -> None:
        """Close any model created by initialize, including a partially loaded one."""

        if self.model is not None:
            self.model.close()

    def prepare_input(self, *, candidate: science.HfInput | science.ChfInput) -> sdk.ScientificReturn[science.PreparationReport]:
        """Return a token-count report while retaining prepared tokens for generation."""

        prepared = science.prepare(model=self.model, candidate=candidate, settings=self.configuration)

        return sdk.ScientificReturn(
            value=science.PreparationReport(token_count=len(prepared.tokens)),
            states=(self.validated.returned(value=prepared),),
        )

    @sdk.operation(state_inputs=(candidate,))
    def generate(self, *, candidate: science.PreparedInput) -> science.Candidates:
        """Consume prepared tokens once and admit the model's bounded output."""

        generated = self.model.generate(tokens=candidate.tokens, beam_size=self.configuration.beam_size)

        science.validate_candidates(candidates=generated, settings=self.configuration)

        return science.Candidates(
            generated_smiles=generated,
            checkpoint_sha256=self.configuration.checkpoint_sha256,
        )


class Hf(PeakContribution):
    """Prepare a formula and proton peaks for generation."""

    interpretation_prompt_file = "hf_prompt.txt"

    @sdk.operation(state_outputs=(PeakContribution.validated,))
    def prepare(self, *, candidate: science.HfInput) -> sdk.ScientificReturn[science.PreparationReport]:
        return self.prepare_input(candidate=candidate)


class Chf(PeakContribution):
    """Prepare a formula and proton and carbon peaks for generation."""

    interpretation_prompt_file = "chf_prompt.txt"

    @sdk.operation(state_outputs=(PeakContribution.validated,))
    def prepare(self, *, candidate: science.ChfInput) -> sdk.ScientificReturn[science.PreparationReport]:
        return self.prepare_input(candidate=candidate)
