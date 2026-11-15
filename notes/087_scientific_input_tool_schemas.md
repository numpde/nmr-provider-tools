# Scientific input schemas and interpretation tools

Research date: 2026-11-15. This extends the scientific/execution boundary in
[086_configured_science_boundary.md](086_configured_science_boundary.md).

## Problem and ownership

The NMRPeak mock defined spectrum inputs twice: once as scientific dataclasses,
then as nested JSON Schema dictionaries selected by a `carbon` boolean. The
language-model tool and the runtime codec could therefore drift independently.

The scientific input type belongs to provider science. Its codec owns structural
JSON admission. Shared interpretation tooling should advertise that codec and
decode tool arguments through it. Provider application code still decides when
to submit validation and generation; a tool declaration does not execute science.

## Research

Pydantic's [TypeAdapter](https://docs.pydantic.dev/latest/concepts/type_adapter/)
supports validation, serialization and schema generation for existing dataclasses;
providers need not copy their records into BaseModel classes. Its
[JSON Schema generator](https://docs.pydantic.dev/latest/concepts/json_schema/)
supports validation schemas, definitions, references and generator customization.

[msgspec](https://msgspec.dev/api) also provides typed serialization and JSON Schema
generation. It is a credible candidate for a broader codec replacement, but
replacing the existing admission implementation is unnecessary for this change.

Handwritten schemas remain reasonable for genuinely external contracts. They are
redundant for fields already owned by these scientific input types. A new general
annotation walker would duplicate established library machinery.

[OpenAI's function-calling guidance](https://developers.openai.com/api/docs/guides/function-calling)
distinguishes a tool declaration from executing its implementation. Strict-mode
schemas require closed objects and every property to be required. Those rules
must not silently erase defaulted-field semantics or fixed-tuple constraints.
Endpoint-specific strict-mode adaptation belongs at the endpoint boundary; this
change preserves the existing non-strict tool carrier.

## Pydantic versus dataclasses

| Option | Provider changes | Construction and admission | Assessment |
|---|---|---|---|
| Standard dataclasses + TypeAdapter for schema only | None to scientific records | Existing constructor, post-init and strict codec remain authoritative | Selected |
| Pydantic dataclasses | Replace decorators or wrap existing classes; configure validation | Validation/conversion precedes post-init; wrapping an existing class can create a distinct subclass | Useful when providers want validated constructors, unnecessary here |
| Pydantic BaseModel | Replace record classes and SDK dataclass contract | Pydantic owns construction, configuration and validation errors | A broader scientific API migration, not this schema fix |
| Full TypeAdapter codec replacement | SDK codec migration; possibly provider configuration | Must reconcile coercion, unknown fields, duplicate keys and constructor errors | Separate decision requiring explicit compatibility proof |

Pydantic's [dataclass documentation](https://docs.pydantic.dev/latest/concepts/dataclasses/)
explains constructor validation, post-init ordering and wrapping ordinary dataclasses.
Its [strict-mode documentation](https://docs.pydantic.dev/latest/concepts/strict_mode/)
explains conversion behavior. The SDK already admits JSON at a separate boundary;
adding validated scientific constructors would duplicate that responsibility and
change direct Python construction. A directly defined frozen/slotted Pydantic
dataclass can satisfy the SDK's dataclass shape, but it is not needed to generate
a schema. Keep ordinary records and use the library where schema translation lives.

## Decision

Use Pydantic TypeAdapter to export the built-in JsonDataclassCodec's input schema.
Keep the codec's existing decoder and encoder. Close generated dataclass objects
to reflect the codec's rejection of unknown fields, including nested objects.
Preserve defaulted optional fields, nullable required fields, fixed and variadic
tuples, Enum/Literal values, and root-level definitions and references. Codec subclasses are not assumed to
retain those semantics and are refused along with other codec implementations.

A shared ScientificInputToolDefinition owns the generated definition and argument decoding. Its name
comes from the selected compiled scientific method. Its parameters are the input
record itself, without an arbitrary `value` envelope. Providers supply the actual
compiled call and a meaningful tool description, not field dictionaries, protocol
names, class-selection flags or duplicate records.

Custom codecs must not silently acquire the built-in codec's JSON semantics.
Unsupported codec/schema capabilities fail before endpoint use. Scientific types
remain ordinary dataclasses. No Pydantic decorators or configuration are added to
provider science.

Pydantic stays in the SDK's existing `api` dependency set. The native installation
and its imports remain free of third-party dependencies; schema generation loads
the library only when requested. Consumers using ScientificInputToolDefinition declare the API extra;
the mock example does so explicitly. The selected version is 2.14.3, with core 2.50.4.
The publisher's advisories affecting dataclass aliases and extra-key validation
were fixed in 2.14.2:
[aliased fields](https://github.com/pydantic/pydantic/security/advisories/GHSA-3r8g-cqwr-jr29),
[extra keys](https://github.com/pydantic/pydantic/security/advisories/GHSA-qpmf-fgh3-ff25).
Exact-version PyPI metadata is checked before adding the pinned dependency closure.

## Limits and proof

JSON Schema does not express every runtime rule. It treats mathematical integers
such as JSON `1.0` differently from this codec's exact integer admission, and
cannot detect duplicate keys after parsing. Scientific validity also requires
scientific execution. Generated schemas guide model input; the codec remains the
runtime admission authority.

Qualification covers generated schemas for nested records, defaults, nullable
fields, tuples and enums; preservation of references; independent exported
documents; local refusal of custom codecs; unchanged strict runtime admission;
and installed HF/CHF requests using the generated tool. Provider-effort review
checks that structural and carrier plumbing have disappeared from the example.
