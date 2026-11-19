"""Download public GitHub release assets without a Git or GitHub CLI identity."""

import pathlib
import reprlib
import shutil
import urllib.parse
import urllib.request


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        """Follow GitHub's asset redirects while retaining HTTPS transport."""

        if not ((redirect_scheme := urllib.parse.urlsplit(url).scheme) == (expected_scheme := "https")):
            raise ValueError(f"{reprlib.repr(redirect_scheme)=} should match {expected_scheme=}")

        return super().redirect_request(request, fp, code, message, headers, url)


def download_asset(
    repository: str,
    tag: str,
    asset_name: str,
    destination: pathlib.Path,
    *,
    timeout_seconds: float,
) -> None:
    """Stream one public asset to an exclusive file; the caller authenticates it."""

    url = f"https://github.com/{repository}/releases/download/{tag}/{asset_name}"  # machine-string: GitHub public release asset route
    opener = urllib.request.build_opener(_HttpsRedirects())

    with opener.open(url, timeout=timeout_seconds) as response, destination.open("xb") as archive:
        shutil.copyfileobj(response, archive)
