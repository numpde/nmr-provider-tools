"""Build the HTTPS redirect policy into the artifact download transport."""

from tools.container_guard import require_container

require_container()

import http.client
import reprlib
import urllib.request
from urllib.parse import urlsplit


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: http.client.HTTPResponse,
        code: int,
        message: str,
        headers: http.client.HTTPMessage,
        new_url: str,
    ) -> urllib.request.Request | None:
        redirect_scheme = urlsplit(new_url).scheme
        expected_scheme = "https"

        if not (redirect_scheme == expected_scheme):
            raise ValueError(f"{reprlib.repr(redirect_scheme)=} should match {expected_scheme=}")

        return super().redirect_request(request, fp, code, message, headers, new_url)


def https_download_opener() -> urllib.request.OpenerDirector:
    """Require HTTPS on redirects from a caller-approved download URL."""

    return urllib.request.build_opener(_HttpsRedirects())
