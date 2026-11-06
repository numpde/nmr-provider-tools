# Provider-core 0.6.0

This release supplies one shared acquisition lifecycle for provider build and download workflows. Thin Bash adapters retain their existing target selection, artifact locks, hashes and publication policy. The common session owns mapped identity, offline workers, lazy interface-bound gateway and isolated relay, ordered worker execution, cancellation and dependency retirement.

The additional `acquisition-owner-0.6.0.tar.gz` release asset contains the complete source bootstrap, including the exact qualified identity dependency and proxy transport. Its member inventory and dependency identity are recorded in the release record, and qualification rebuilds the exact archive bytes. Consumers verify their checked-in bootstrap against that record before use; cold acquisition needs no ambient host package installation.

Worker results are available only after confirmed retirement. An unresolved worker retains its relay and gateway; unresolved relay retirement retains its gateway. A fresh retirement receipt authorizes releasing caller-owned inputs. Ordinary callback failure and actual cancellation retain their original status, while caller traps and live worker output remain usable.

The scientific SDK gains no mandatory identity dependency. Existing API proxy and outbox behavior is unchanged. Networked BuildKit acquisition and daemon bootstrap-image pulls retain the boundaries documented in note 045.
