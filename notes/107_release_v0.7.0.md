# provider-core 0.7.0

Scientific JSON records now use the pinned Pydantic `TypeAdapter` for validation,
serialization, and schema generation. The handwritten type walkers are removed.
This supports unions, inherited `TypedDict` fields with required and optional keys,
nested dataclasses, and JSON metadata. Schemas, encoding, and decoding consistently
use declared field names. Duplicate keys, nonfinite JSON numbers, and unexpected
record fields remain rejected.

Pydantic is now a native runtime dependency. The native installation lock includes
its dependency closure, derived from the SDK wheel metadata with pip and verified
against the existing runtime lock. Native workers should install the issued native
lock rather than installing the SDK wheel alone with `--no-deps`.

Validation failures use `PayloadRejected.Code.INVALID_VALUE` and Pydantic's JSON
diagnostics, omitting input values and context from that message while preserving
the original cause. The previous `INVALID_TYPE`, `INVALID_LENGTH`, `UNEXPECTED_FIELD`,
and `MISSING_FIELD` categories are removed. Serialization failures retain their
`PydanticSerializationError` rather than being rewrapped as `TypeError`.

Scientific binding revisions now hash the generated JSON schemas and use the
`configured-v4` prefix. Changes to inherited `TypedDict` fields, required keys, and
other represented constraints therefore participate in binding identity. Existing
revision strings remain parseable; this does not change the native runner protocol.

A known-risk test records Pydantic accepting JSON integer 1 as boolean True for
`Literal[True, 2]`, even in strict mode. The SDK does not add a second type validator
to override this library behavior. Replace the current-behavior test with a
normative rejection test when the upstream implementation changes.

Attached qualification logs identify the exact artifacts tested and privacy-test
skips. Repository quality debt remains tracked in `notes/092_repo_checks_status.html`;
real ChefNMR2 model integration is separate qualification.
