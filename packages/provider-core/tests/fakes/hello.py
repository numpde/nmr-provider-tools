"""Authored announcement and receipt from the pinned API Hello schemas."""

import json

from provider_core.hello import AnalysisOffering


def receipt(**changes):
    return json.dumps(
        {
            "schema_id": "nmr.provider.hello_response.v1",
            "provider_ref": "provider:example",
            "accepted_at": "2026-10-20T10:00:00.123456Z",
            **changes,
        }
    ).encode()


def announcement(**changes):
    return {
        "provider_ref": "provider:example",
        "display_name": "Example provider",
        "description": "Local test service",
        "analysis_offerings": (AnalysisOffering("nmr_predict", "Test analysis"),),
        **changes,
    }
