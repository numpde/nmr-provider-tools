"""Require owned types for identifier-shaped string inputs.

Inspect class fields and constructor parameters ending in ``code``, ``_id`` or
``_ref``, plus bare ``id``. Numeric process statuses and IDs remain numeric;
this rule flags unrestricted strings, Any/object and missing annotations. Enums,
nominal types and closed Literal declarations provide an explicit owner.

Aliases, optional types and forward annotations do not hide an unrestricted
string. Derived dataclass fields (init=False), enum members and ClassVar
constants are declarations or projections rather than constructor inputs.

An intentional wire/text input needs a real inline comment on its declaration:
``# repo-check: allow-identifier-type -- <boundary and reason>``. The escape
covers only that declaration's line, never a class or file. Empty, unattached
and unnecessary escapes are findings. This is a naming-based posture check;
runtime constructors and normal type checking still own actual value admission.
"""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import io
import pathlib
import tokenize
import unittest

from operations.checker.findings import assert_no_findings
from .sources import ROOT, python_sources
from .test_error_code_enum_posture import _aliases, _module_name, _qualified_name


def _identifier(name: str) -> bool:
    name = name.casefold()
    return name == "id" or name.endswith(("code", "_id", "_ref"))


def _scope_statements(body):
    """Include conditional declarations without crossing a Python scope."""
    pending = list(reversed(body))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.stmt):
            yield node
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


class _Annotations:
    """Resolve declaration aliases without pretending to infer Python dataflow."""

    def __init__(self, modules):
        self.contexts = {}
        self.bindings = {}
        self.type_parameters = {}
        self.classes = {}
        self.enums = {"enum.Enum", "enum.StrEnum", "enum.IntEnum", "enum.Flag", "enum.IntFlag"}

        for path, module in modules.items():
            self._scope(module, module.body, {}, _module_name(path), path)

        while True:
            found = {
                name for name, (declaration, aliases) in self.classes.items()
                if any(self.name(base, aliases) in self.enums for base in declaration.bases)
            }
            if found <= self.enums:
                break
            self.enums.update(found)
        self.enum_nodes = {node for name, (node, _) in self.classes.items() if name in self.enums}

    def name(self, expression, aliases, seen=frozenset()):
        name = _qualified_name(expression, aliases)
        if name in self.bindings and name not in seen:
            value, owner_aliases = self.bindings[name]
            if isinstance(value, (ast.Name, ast.Attribute)):
                return self.name(value, owner_aliases, seen | {name})
        return name

    def _scope(self, node, body, inherited, prefix, path):
        body = list(_scope_statements(body))
        aliases = inherited | _aliases(ast.Module(body=body, type_ignores=[]), path)
        self.contexts[node] = aliases

        for statement in body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                aliases[statement.name] = prefix + "." + statement.name
            elif isinstance(statement, ast.TypeAlias):
                aliases[statement.name.id] = prefix + "." + statement.name.id
                self.bindings[aliases[statement.name.id]] = (statement.value, aliases)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name) and statement.value is not None:
                        aliases[target.id] = prefix + "." + target.id
                        self.bindings[aliases[target.id]] = (statement.value, aliases)

        for parameter in getattr(node, "type_params", ()):
            aliases[parameter.name] = prefix + "." + parameter.name
            bound = getattr(parameter, "bound", None) or ast.Attribute(value=ast.Name(id="typing", ctx=ast.Load()), attr="Any", ctx=ast.Load())
            self.bindings[aliases[parameter.name]] = (bound, aliases)

        for statement in body:
            if isinstance(statement, ast.TypeAlias):
                name = aliases[statement.name.id]
                self._scope(statement, [], aliases, name, path)
                owner_aliases = self.contexts[statement]
                self.bindings[name] = (statement.value, owner_aliases)
                self.type_parameters[name] = [owner_aliases[parameter.name] for parameter in statement.type_params]
            elif isinstance(statement, ast.ClassDef):
                self.classes[aliases[statement.name]] = (statement, aliases)
                self._scope(statement, statement.body, aliases, aliases[statement.name], path)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._scope(statement, statement.body, aliases, aliases[statement.name], path)

    def problem(self, annotation, aliases, seen=frozenset(), bindings=None):
        if bindings is None:
            bindings = self.bindings
        if annotation is None:
            return "has no declared type"
        if isinstance(annotation, ast.Constant):
            if annotation.value is None:
                return None
            if isinstance(annotation.value, str):
                try:
                    forward = ast.parse(annotation.value, mode="eval").body
                except SyntaxError:
                    return "has an invalid forward annotation"
                return self.problem(forward, aliases, seen, bindings)
        if isinstance(annotation, (ast.Name, ast.Attribute)):
            name = _qualified_name(annotation, aliases)
            if name in {"str", "builtins.str", "object", "builtins.object", "Any", "AnyStr", "LiteralString", "typing.Any", "typing.AnyStr", "typing.LiteralString", "typing_extensions.Any", "typing_extensions.AnyStr", "typing_extensions.LiteralString"}:
                return "permits unrestricted strings or untyped values"
            if name in bindings:
                if name in seen:
                    return "has a cyclic type alias"
                value, owner_aliases = bindings[name]
                return self.problem(value, owner_aliases, seen | {name}, bindings)
            return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self.problem(annotation.left, aliases, seen, bindings) or self.problem(annotation.right, aliases, seen, bindings)
        if isinstance(annotation, ast.Tuple):
            return next((problem for value in annotation.elts if (problem := self.problem(value, aliases, seen, bindings))), None)
        if isinstance(annotation, ast.Starred):
            return self.problem(annotation.value, aliases, seen, bindings)
        if isinstance(annotation, ast.Subscript):
            name = _qualified_name(annotation.value, aliases)
            if name in {"typing.Literal", "typing_extensions.Literal", "typing.ClassVar", "typing_extensions.ClassVar", "typing.Callable", "collections.abc.Callable", "type", "builtins.type"}:
                return None
            values = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            if name in bindings:
                if name in seen:
                    return "has a cyclic type alias"
                value, owner_aliases = bindings[name]
                arguments = {
                    parameter: (argument, aliases)
                    for parameter, argument in zip(self.type_parameters.get(name, ()), values)
                }
                return self.problem(value, owner_aliases, seen | {name}, bindings | arguments)
            if name in {"typing.Annotated", "typing_extensions.Annotated"}:
                values = values[:1]
            return next((problem for value in values if (problem := self.problem(value, aliases, seen, bindings))), None)
        if isinstance(annotation, ast.Call):
            name = _qualified_name(annotation.func, aliases)
            if name in {"typing.NewType", "typing_extensions.NewType"}:
                return None
            if name in {"typing.TypeVar", "typing_extensions.TypeVar"}:
                constraints = [*annotation.args[1:], *(item.value for item in annotation.keywords if item.arg == "bound")]
                if not constraints:
                    return "uses an unconstrained type variable"
                return next((problem for value in constraints if (problem := self.problem(value, aliases, seen, bindings))), None)
        return None


def _keyword_false(call, name):
    return isinstance(call, ast.Call) and any(
        item.arg == name and isinstance(item.value, ast.Constant) and item.value.value is False
        for item in call.keywords
    )


def _declarations(module, annotations):
    for declaration in ast.walk(module):
        if not isinstance(declaration, ast.ClassDef):
            continue
        aliases = annotations.contexts[declaration]
        if declaration in annotations.enum_nodes:
            continue
        dataclass_decorators = [
            decorator for decorator in declaration.decorator_list
            if annotations.name(decorator.func if isinstance(decorator, ast.Call) else decorator, aliases) == "dataclasses.dataclass"
        ]
        derived = any(_keyword_false(decorator, "init") for decorator in dataclass_decorators)
        for field in _scope_statements(declaration.body):
            if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name) and not derived:
                derived_field = (
                    dataclass_decorators and isinstance(field.value, ast.Call) and
                    annotations.name(field.value.func, aliases) == "dataclasses.field" and
                    _keyword_false(field.value, "init")
                )
                if _identifier(field.target.id) and not derived_field:
                    yield field, field.target.id, field.annotation, aliases
            if isinstance(field, (ast.FunctionDef, ast.AsyncFunctionDef)) and field.name == "__init__":
                parameters = (*field.args.posonlyargs, *field.args.args, *field.args.kwonlyargs, field.args.vararg, field.args.kwarg)
                for parameter in parameters:
                    if parameter is not None and _identifier(parameter.arg):
                        yield parameter, parameter.arg, parameter.annotation, annotations.contexts[field]

    parents = {child: node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}
    for call in ast.walk(module):
        if not isinstance(call, ast.Call) or len(call.args) < 2:
            continue
        scope = call
        while scope not in annotations.contexts:
            scope = parents[scope]
        aliases = annotations.contexts[scope]
        factory = annotations.name(call.func, aliases)
        fields = call.args[1]
        if factory in {"typing.TypedDict", "typing_extensions.TypedDict"} and isinstance(fields, ast.Dict):
            pairs = zip(fields.keys, fields.values)
        elif factory in {"typing.NamedTuple", "typing_extensions.NamedTuple"} and isinstance(fields, (ast.List, ast.Tuple)):
            pairs = (field.elts for field in fields.elts if isinstance(field, (ast.Tuple, ast.List)) and len(field.elts) == 2)
        else:
            continue
        for name, annotation in pairs:
            if isinstance(name, ast.Constant) and isinstance(name.value, str) and _identifier(name.value):
                yield annotation, name.value, annotation, aliases


def identifier_type_findings(*, sources: dict[pathlib.Path, str]) -> list[tuple[pathlib.Path, int, str]]:
    """Report unowned identifier inputs and invalid local waiver comments."""
    modules = {path: ast.parse(source) for path, source in sources.items()}
    annotations = _Annotations(modules)
    findings = []
    prefix = "# repo-check: allow-identifier-type -- "

    for path, source in sources.items():
        escapes = {}
        used = set()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT or not token.string.startswith("# repo-check: allow-identifier-type"):
                continue
            if not token.line[:token.start[1]].strip():
                findings.append((path, token.start[0], "identifier-type escape must trail its declaration"))
            elif not token.string.startswith(prefix) or not token.string[len(prefix):].strip():
                findings.append((path, token.start[0], "identifier-type escape requires '--' and a nonempty boundary/reason"))
            else:
                escapes[token.start[0]] = token

        problems = []
        for node, name, annotation, aliases in _declarations(modules[path], annotations):
            problem = annotations.problem(annotation, aliases)
            if problem is None:
                continue
            declaration_lines = {node.lineno, node.end_lineno}
            if annotation is not None:
                declaration_lines.add(annotation.end_lineno)
            spelling = ast.unparse(annotation) if annotation is not None else "unannotated"
            reason = f"identifier declaration {name!r} ({spelling}) {problem}; use an owned type or an inline {prefix}<reason>"
            problems.append((declaration_lines, node.lineno, reason))

        suppressed = set()
        for line in escapes:
            candidates = [index for index, (lines, _, _) in enumerate(problems) if line in lines]
            if len(candidates) == 1:
                suppressed.add(candidates[0])
                used.add(line)
            elif len(candidates) > 1:
                findings.append((path, line, "identifier-type escape matches multiple declarations; put each declaration on its own line"))
                used.add(line)

        for index, (_, line, reason) in enumerate(problems):
            if index not in suppressed:
                findings.append((path, line, reason))

        for line in escapes.keys() - used:
            findings.append((path, line, "identifier-type escape is not attached to a flagged identifier declaration"))

    return sorted(findings)


class IdentifierTypePostureTests(unittest.TestCase):
    def findings(self, source):
        return identifier_type_findings(sources={pathlib.Path("example.py"): source})

    def test_rejects_string_identifiers_independent_of_class_name_or_inheritance(self):
        source = '''from dataclasses import dataclass
from typing import NamedTuple, TypedDict, Any
@dataclass
class ArbitraryRecord:
    code: str
    result_schema_id: str
    provider_ref: str
    barcode: Any
class OtherRecord(NamedTuple):
    owner_id: str | None
class Document(TypedDict):
    id: object
class Plain:
    def __init__(self, postcode: str, operation_id):
        pass
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [5, 6, 7, 8, 10, 12, 14, 14])

    def test_aliases_optional_types_and_forward_annotations_do_not_hide_strings(self):
        source = '''from typing import Optional as Maybe, Any as Unknown, TypeAlias
Text: TypeAlias = str
type Indirect = Text
class Record:
    external_id: "Maybe[Indirect]"
    error_code: Unknown
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [5, 6])

    def test_imported_string_aliases_are_resolved_across_selected_sources(self):
        sources = {
            pathlib.Path("aliases.py"): "type Text = str\n",
            pathlib.Path("consumer.py"): "from aliases import Text as Identity\nclass Record:\n    result_id: Identity\n",
        }
        self.assertEqual([path for path, _, _ in identifier_type_findings(sources=sources)], [pathlib.Path("consumer.py")])

    def test_generic_aliases_inspect_their_definition_with_supplied_arguments(self):
        source = '''type Erased[T] = str
type Identity[T] = T
type OptionalIdentity[T] = Identity[T] | None
class Record:
    erased_id: Erased[OwnerId]
    owner_id: OptionalIdentity[OwnerId]
    bad_id: OptionalIdentity[str]
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [5, 7])

    def test_conditionally_declared_fields_and_variadic_constructor_inputs_are_checked(self):
        source = '''from typing import TYPE_CHECKING
class Record:
    if TYPE_CHECKING:
        owner_id: str
    if enabled:
        def __init__(self, *source_code: str, **document_id):
            pass
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [4, 6, 6])

    def test_functional_typed_records_do_not_hide_identifier_fields(self):
        source = '''from typing import TypedDict as document, NamedTuple as record
Wire = document("Wire", {"schema_id": str, "description": str})
Result = record("Result", [("code", str), ("owner_id", OwnerId)])
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [2, 3])

    def test_accepts_owned_types_finite_literals_and_numeric_observations(self):
        source = '''from enum import Enum, auto
from typing import ClassVar, Literal, NewType
class State(Enum):
    ERROR_CODE = "error"
RecordId = NewType("RecordId", str)
class Record:
    error_code: State
    owner_id: RecordId
    result_id: EncodedPayload
    codec: PayloadCodec
    returncode: int | None
    phase_code: Literal["waiting", "running"]
    schema_id: ClassVar[str] = "declared.v1"
    description: str
    format: str
'''
        self.assertEqual(self.findings(source), [])

    def test_derived_fields_are_exempt_but_custom_constructor_inputs_are_checked(self):
        source = '''from dataclasses import dataclass as record, field as derived
@record
class Projection:
    schema_id: str = derived(init=False)
@record(init=False)
class OtherProjection:
    schema_id: str
    def __init__(self, source_id: str):
        pass
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [8])

    def test_init_false_on_an_unrelated_factory_does_not_hide_a_string(self):
        source = '''class Record:
    schema_id: str = unrelated(init=False)
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [2])

    def test_unbounded_generic_parameters_cannot_hide_string_identities(self):
        source = '''from typing import TypeVar
T = TypeVar("T")
class Record:
    owner_id: T
class GenericRecord[U]:
    schema_id: U
class StringRecord[V: str]:
    code: V
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [4, 6, 8])

    def test_conditional_and_nested_declarations_keep_their_local_aliases(self):
        source = '''if enabled:
    from typing import Any as Unknown
    class Record:
        error_code: Unknown
def build():
    Text = str
    if enabled:
        class Other:
            owner_id: Text
        return Other
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [4, 9])

    def test_variadic_generic_parameters_are_reported_without_crashing(self):
        source = '''class Pack[*Parts]:
    owner_id: tuple[*Parts]
class Callback[**Arguments]:
    code: Arguments
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [2, 4])

    def test_escape_is_inline_reasoned_and_limited_to_its_declaration(self):
        source = '''class Wire:
    failure_code: str  # repo-check: allow-identifier-type -- API wire field admitted by the response parser.
    schema_id: str
    def __init__(self,
                 source_code: str,  # repo-check: allow-identifier-type -- Python program text consumed by compile().
                 owner_id: str):
        pass
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [3, 6])

    def test_strings_and_class_level_comments_cannot_waive_declarations(self):
        source = '''marker = "# repo-check: allow-identifier-type -- not a comment"
# repo-check: allow-identifier-type -- not a field declaration
class Wire:
    failure_code: str
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [2, 4])

    def test_comment_in_an_initializer_cannot_waive_the_outer_field(self):
        source = '''class Record:
    schema_id: str = build(
        option=1,  # repo-check: allow-identifier-type -- this belongs to an inner call
    )
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [2, 3])

    def test_one_escape_cannot_waive_multiple_declarations_on_the_same_line(self):
        source = '''class Record:
    first_id: str; second_id: str  # repo-check: allow-identifier-type -- ambiguous fields
    def __init__(self, owner_id: str, source_id: str):  # repo-check: allow-identifier-type -- ambiguous parameters
        pass
'''
        self.assertEqual([line for _, line, _ in self.findings(source)], [2, 2, 2, 3, 3, 3])

    def test_empty_and_stale_escapes_are_findings(self):
        source = '''class Wire:
    code: str  # repo-check: allow-identifier-type --
    owner_id: OwnerId  # repo-check: allow-identifier-type -- no longer needed
'''
        findings = self.findings(source)
        self.assertEqual([line for _, line, _ in findings], [2, 2, 3])

    def test_identifier_inputs_have_owned_types(self):
        findings = identifier_type_findings(sources={
            path.relative_to(ROOT): path.read_text(encoding="utf-8")
            for path in python_sources(production_only=False)
        })
        assert_no_findings([f"{path}:{line}: {reason}" for path, line, reason in findings])
