"""Construct the public GitHub release asset route used by delivery acquisition."""

from tools.container_guard import require_container

require_container()


def release_asset_url(repository: str, tag: str, asset_name: str) -> str:
    """Build the URL from the repository, tag and asset selected by the caller."""

    return f"https://github.com/{repository}/releases/download/{tag}/{asset_name}"  # machine-string: GitHub routes by repository, tag and asset path segments
