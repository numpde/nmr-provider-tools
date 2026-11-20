"""Construct NMR store GETs and bind protocol rejection to transfer evidence."""

from provider_core.container_guard import require_container

require_container()

import reprlib

import httpx

from provider_core.response_json import ResponseRejected
from provider_core.http_response import HttpResponse, HttpResponseHead
from provider_core.upload_file import UploadFailed
from provider_core.validation.upstream.nmr.v1.upload_response import ReadGrant
from provider_core.validation.upstream.nmr.v1.upload_store import require_download_capability, require_store_headers


def download_target(store_origin: str, upload_ref: str) -> str:
    """Render the transfer path from a locally granted origin and admitted ref."""

    return store_origin + "/upload/v1/uploads/" + upload_ref + "/bytes"


def download_request(target: str, *, capability: str) -> httpx.Request:
    """Build a GET after the caller binds its URL and checks current expiry."""

    try:
        require_download_capability(capability)
    except ResponseRejected as rejection:
        raise UploadFailed(str(rejection)) from None

    download_http_request = httpx.Request("GET", target, headers={"Authorization": "Bearer " + capability})

    if str(download_http_request.url) != target:
        raise UploadFailed(f"{reprlib.repr(str(download_http_request.url))=} should match {reprlib.repr(target)=}")

    return download_http_request


def verify_store_headers(response: HttpResponse | HttpResponseHead, grant: ReadGrant) -> None:
    """Bind before-body or late-head admission to the original transfer head."""

    try:
        require_store_headers(response, grant)
    except ResponseRejected as rejection:
        raise UploadFailed(str(rejection), response=response) from None
