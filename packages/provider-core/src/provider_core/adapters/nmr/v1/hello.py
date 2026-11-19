"""Encode composition's current complete announcement as exact NMR v1 JSON."""

from provider_core.container_guard import require_container

require_container()

import json

from provider_core.validation.upstream.nmr.v1.hello import require_hello_body_size, require_hello_inputs


def hello_body(*, display_name, description, analysis_offerings):
    """Encode the caller's current complete snapshot, including empty offerings.

    Preserve permitted whitespace and offering order. UTF-8 avoids expanding
    supplementary scalars into surrogate escapes near the route's byte ceiling.
    No prepared request object or replay obligation survives this construction.
    """

    require_hello_inputs(
        display_name=display_name,
        description=description,
        analysis_offerings=analysis_offerings,
    )

    offerings = [
        {"analysis_kind_ref": offering.analysis_kind_ref, "description": offering.description}
        for offering in analysis_offerings
    ]

    body = (
        json
        .dumps(
            {
                "schema_id": "nmr.provider.hello_request.v1",
                "display_name": display_name,
                "description": description,
                "analysis_offerings": offerings,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        .encode("utf-8")
    )

    require_hello_body_size(body)

    return body
