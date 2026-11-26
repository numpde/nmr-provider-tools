# provider-core 0.7.8

2026-11-26. SDK releases now include the exact wheel METADATA as a `.whl.metadata` asset and a single-release HTML package listing. The listing records both wheel and metadata SHA-256 hashes, allowing metadata-only dependency resolution while retaining wheels in GitHub Releases.

Release qualification checks these discovery artifacts against the built wheel and includes them in the release manifests and downloaded-asset comparison. After publication succeeds, the publisher atomically updates `simple/provider-core.html` in the checkout. Commit and push that generated file, then pin its raw GitHub URL to the listing commit. Failed or uncertain publication preserves the previous listing; a listing-write failure after successful publication reports how to recover without republishing the release.

The bundled acquisition owner also includes the upstream-proxy support added since 0.7.7: explicit HTTP and SOCKS proxy routes, remote DNS through the selected proxy, and no direct fallback on proxy refusal. Installed-artifact qualification exercises the extracted proxy implementation using tests captured from the same source commit.
