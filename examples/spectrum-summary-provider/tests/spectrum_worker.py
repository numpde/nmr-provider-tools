"""Witness installed science and inject a producer encoder failure in its process."""

from dataclasses import asdict
import json
import os
import pathlib
from unittest.mock import patch

from provider_core.container_guard import require_container
from provider_core.payload import JsonDataclassCodec
from provider_core.process_worker import main
from spectrum_provider import science

require_container()


def record(event, spectrum):
    with pathlib.Path(os.environ["SPECTRUM_WITNESS"]).open("a", encoding="utf-8") as witness:
        witness.write(json.dumps({"event": event, "pid": os.getpid(), "spectrum": asdict(spectrum)}) + "\n")


if __name__ == "__main__":
    subtract_baseline = science.subtract_baseline
    detect_peaks = science.detect_peaks
    encode = JsonDataclassCodec.encode

    def witnessed_baseline(*, spectrum, settings):
        record("baseline", spectrum)

        return subtract_baseline(spectrum=spectrum, settings=settings)

    def witnessed_peaks(*, spectrum, settings):
        record("peaks", spectrum)

        return detect_peaks(spectrum=spectrum, settings=settings)

    def encode_spectrum(codec, scientific_result):
        if codec.value_type is science.Spectrum:
            record("encode", scientific_result)

            if not (os.environ["SPECTRUM_ENCODING_AVAILABLE"] == "1"):
                raise OSError(f"Cannot encode calculated spectrum; {scientific_result=}")

        return encode(codec, scientific_result)

    with (
        patch.object(science, "subtract_baseline", witnessed_baseline),
        patch.object(science, "detect_peaks", witnessed_peaks),
        patch.object(JsonDataclassCodec, "encode", encode_spectrum),
    ):
        main()
