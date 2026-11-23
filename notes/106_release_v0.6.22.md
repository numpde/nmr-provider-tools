# provider-core 0.6.22

The default JSON codec now accepts ordinary dataclasses, including records without `frozen=True` or `slots=True`. Scientific fields can use `list[T]` and `dict[str, T]` alongside existing tuples and nested dataclasses. Providers no longer need to replace these JSON-compatible structures merely to use SDK-managed serialization.

Encoding validates the supplied Python values; decoding validates supplied JSON fields and reconstructs nested dataclasses. Omitted optional fields use the dataclass defaults. Dictionary keys must be strings; arbitrary Python objects, `Any`, `object`, and unparameterized lists or dictionaries remain unsupported. Duplicate JSON keys, unexpected dataclass fields, invalid scalar types, and nonfinite numbers remain errors. Schema export describes typed list elements and dictionary values while keeping dataclass fields closed.

This release changes the accepted scientific value types without changing the native runner protocol. Existing frozen/slotted records and tuples remain supported. Regression coverage includes mutable and immutable dataclass combinations, nested list/dictionary round trips, independent decoded values and defaults, malformed data, and schema export. Attached qualification logs identify the exact artifacts tested and any privacy-test skips; real-model integration is separate work.
