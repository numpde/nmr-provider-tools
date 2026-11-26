# Provider SDK v0.7.9

SDK workflows use pinned pip and uv directly with hashed requirements locks. SDK releases no longer build, publish or require a Wheelhouse tool. Acquisition, offline checks and installed tests share the maintained requirements locks; bootstrap dependencies retain exact wheel URLs and hashes. The existing released SDK artifacts remain immutable.

The acquisition archive includes a standard-library context exporter. Consumers supply exact source paths, destinations, hashes and executable modes; the exporter streams only those files and verifies the emitted bytes. Failed writes, cancellation, mutation or builder failure prevent a successful delivery publication.

SDK lock refresh derives runtime and build requirements from the package metadata and test-only requirements from their direct input. Bootstrap dependencies keep their exact source declarations. The checker image includes pinned uv for this explicit refresh lane. Existing baseline versions and wheel hashes are preserved by the initial migration.

The acquisition archive and release metadata no longer carry a Wheelhouse release dependency. Consumer runtime dependency upgrades and deployment changes are separate work.

Qualification covers SDK artifact rebuilding, installed SDK tests, acquisition and lock refresh, and an offline shared delivery image build. It uses the established release setting `PRIVACY_TESTS=0`; skipped privacy checks are identified in the attached log and are not passing results. Provider repositories, including ChefNMR2, have not yet been qualified against this release.
