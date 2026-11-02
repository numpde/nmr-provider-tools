# provider-core 0.5.8

The independent wheelhouse tool can now project an existing flat pinned
requirements lock into URL-bearing artifact metadata. It reads package-index
metadata, intersects it with the lock's existing SHA-256 hashes, and preserves
explicitly pinned direct wheel URLs. It does not resolve dependencies or fetch
wheel bodies during projection.

Run projection in the intended target environment. Existing lock owners must
provide their own direct source pins and required source-distribution policy;
the shared publisher verifies the selected artifact bytes when acquiring them.
