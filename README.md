# NMR provider tools

This repository will own model-independent provider infrastructure: the shared
`provider_core` package, durable result delivery, and delivery deployment tooling.
Providers retain scientific input preparation, inference, model runners,
offerings and model configuration.

The current checkout contains the repository-check port. The provider package
and delivery service have not been extracted yet.

Run `make help` to discover commands. `make tests` runs every current check;
`make tests/repository/quality` narrows the run to convention detectors and their
fixtures. `make tests/repository/runtime` verifies the actual checker container.
`make tests/repository/command` prints the launch command without execution.

The lane requires Bash, GNU find, a non-root caller, and Docker at
`unix:///var/run/docker.sock`. It uses the immutable local image ID in
`config/checker-image.txt`; execution never downloads or builds an image. A host
missing that image must load the exact checker artifact before running
the lane. NMRNet recorded this image ID; the donor recipe revision and observed
tool versions are recorded in
[the port record](notes/001_repository_check_port_20261026.txt).

Checks run without networking, credentials, an engine socket or writable source.
Public root entries are mounted read-only; private control and state directories
are excluded and represented only by names and types in a temporary inventory.
The checker receives a non-root UID/GID, a read-only root filesystem, no Linux
capabilities, no-new-privileges, two CPUs, 512 MiB memory, no swap, 64 PIDs and a
64 MiB non-executable temporary filesystem. Host Bash creates and removes its
private temporary engine-client directory. Containers share the host kernel;
these restrictions do not establish a hostile-code sandbox.

Repository findings may remain red. A missing tool, failed import or failed
container launch is a failed proof prerequisite, not a passing check or a
convention finding. Preserve detector rejection cases when correcting a port.
The [source manifest](tests/repository/port-sources.json) identifies donor bytes;
the [dispositions](tests/repository/port-dispositions.json) explain donor-specific
test methods replaced at the local boundary.

Ruff formatting is owned by the separate `ruffer` checkout. This repository
keeps its custom convention and import-order checks and does not install or
invoke Ruff. Ruffer currently provides single-file formatting lanes; this
separation does not claim repository-wide Ruff lint coverage.
