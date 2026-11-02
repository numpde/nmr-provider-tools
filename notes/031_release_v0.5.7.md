# provider-core 0.5.7

This release adds a separately runnable shared wheelhouse tool to the release
assets. It selects artifacts for the Python environment that runs it, reuses or
acquires hash-locked files without replacing existing pool entries, and can
stream selected build inputs without staging wheel copies.

Build callers must check both the stream producer and the container build before
admitting an image. Providers select their own target environment and retain
their existing lock ownership. The tool does not itself upgrade provider builds
or prove a complete provider migration.
