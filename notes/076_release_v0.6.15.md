# provider-core 0.6.15

The SDK owns the installer used by acquisition, wheel selection, package builds, and offline installation. The cold acquisition bundle supplies image-build inputs: the standard-library verifier and environment helper, its shared container guard, the pip 26.2.2 release identity, and its exact wheel. Consumer refresh verifies the existing snapshot against its recorded release before replacing it.

Consuming Dockerfiles install verified pip during image assembly. Images that create environments retain the approved wheel and release record at the SDK-owned image path. The common environment helper creates a venv without running bundled ensurepip, authenticates a private copy of the image-owned wheel, and installs approved pip into the target through pip's public interpreter-selection interface. Subsequent operations use the target's ordinary python -m pip command, retaining borrowed pool-lock descriptors in the process reading the shared cache. Installer identity remains separate from application dependency locks.

SDK source acquisition prepares the checker bootstrap image before running acquisition helpers. Wheelhouse image builds require acquired inputs and no longer download a missing streamer. Image streams identify installer inputs separately from scientific runtime locks. Fixed installer code and inputs belong to their consuming images rather than runtime host-file mounts.

Behavioral regressions build a real wheel through a PEP 517 backend, install it from a hash lock, verify the target's installer version and location, and reject corrupt and symlinked installer inputs. A native disposable-context check exercises explicit endpoint selection against contradictory ambient selectors and a read-only cache check with an invalid network selector.

The acquisition bundle retains the exact published linux-idmap 0.3.3 dependency.
