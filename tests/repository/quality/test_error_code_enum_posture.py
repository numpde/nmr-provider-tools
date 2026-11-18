"""Check authored exception declarations and direct constructor calls.

Use at most one positional message. Pass error codes by keyword as members of
the exception's own nested Code enum, which derives from the shared ErrorCode.
"""

from tools.container_guard import require_container

require_container()

import ast
import builtins
import pathlib
import unittest

from .sources import ROOT, python_sources


def _module_name(source_path: pathlib.Path) -> str:
    parts = source_path.with_suffix("").parts

    if parts[0] == "src":
        parts = parts[1:]
    elif parts[0] in {"packages", "examples", "mocks"} and parts[2:3] == ("src",):
        parts = parts[3:]
    elif parts[:3] == ("packages", "provider-core", "tests"):
        parts = parts[2:]

    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _qualified_name(expression: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(expression, ast.Name):
        return aliases[expression.id] if expression.id in aliases else expression.id

    if isinstance(expression, ast.Attribute):
        return f"{_qualified_name(expression.value, aliases)}.{expression.attr}"

    return ""


def _aliases(module: ast.Module, source_path: pathlib.Path) -> dict[str, str]:
    module_name = _module_name(source_path)
    package = module_name if source_path.name == "__init__.py" else module_name.rpartition(".")[0]
    aliases = {}

    for statement in module.body:
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                if imported.asname is not None:
                    aliases[imported.asname] = imported.name
        elif isinstance(statement, ast.ImportFrom):
            if statement.level:
                package_parts = package.split(".")
                prefix = package_parts[:len(package_parts) - statement.level + 1]
                imported_module = ".".join((*prefix, statement.module) if statement.module else prefix)
            else:
                imported_module = statement.module

            for imported in statement.names:
                aliases[imported.asname or imported.name] = f"{imported_module}.{imported.name}"
        elif isinstance(statement, ast.ClassDef):
            aliases[statement.name] = f"{module_name}.{statement.name}"

    return aliases


def _method(declaration: ast.ClassDef, method_name: str) -> ast.FunctionDef | None:
    return next(
        (method for method in declaration.body if isinstance(method, ast.FunctionDef) and method.name == method_name),
        None,
    )


def _lowercases_member_name(generator: ast.FunctionDef | None) -> bool:
    if generator is None:
        return False

    parameters = (*generator.args.posonlyargs, *generator.args.args)

    if not parameters:
        return False

    returns = [statement.value for statement in generator.body if isinstance(statement, ast.Return)]

    return len(returns) == 1 and ast.unparse(returns[0]) == f"{parameters[0].arg}.lower()"


def _constructor_parameters(declaration: ast.ClassDef) -> tuple[tuple[ast.arg, ...], tuple[ast.arg, ...]] | None:
    constructor = _method(declaration, "__init__")

    if constructor is None:
        return None

    return ((*constructor.args.posonlyargs, *constructor.args.args)[1:], tuple(constructor.args.kwonlyargs))


def _exception_parameters(
    declarations: dict[str, tuple[pathlib.Path, ast.ClassDef]],
    aliases_by_source: dict[pathlib.Path, dict[str, str]],
) -> dict[str, tuple[tuple[ast.arg, ...], tuple[ast.arg, ...]]]:
    builtin_exceptions = {
        name for (name, candidate) in vars(builtins).items()
        if isinstance(candidate, type) and issubclass(candidate, BaseException)
    }

    inherited = {name: ((), ()) for name in builtin_exceptions}

    inherited.update({f"builtins.{name}": ((), ()) for name in builtin_exceptions})

    while True:
        discovered = {}

        for (name, (source_path, declaration)) in declarations.items():
            if name in inherited:
                continue

            bases = [
                _qualified_name(parent, aliases_by_source[source_path])
                for parent in declaration.bases
            ]

            exception_parents = [parent for parent in bases if parent in inherited]

            if exception_parents:
                own_parameters = _constructor_parameters(declaration)
                discovered[name] = inherited[exception_parents[0]] if own_parameters is None else own_parameters

        if not discovered:
            return {name: parameters for (name, parameters) in inherited.items() if name in declarations}

        inherited.update(discovered)


def _nested_code(declaration: ast.ClassDef) -> ast.ClassDef | None:
    return next(
        (member for member in declaration.body if isinstance(member, ast.ClassDef) and member.name == "Code"),
        None,
    )


def _class_assignments(declaration: ast.ClassDef) -> list[tuple[ast.Name, ast.expr | None]]:
    assignments = []

    for statement in declaration.body:
        if isinstance(statement, ast.Assign):
            assignments.extend((target, statement.value) for target in statement.targets if isinstance(target, ast.Name))
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            assignments.append((statement.target, statement.value))

    return assignments


def error_code_findings(*, sources: dict[pathlib.Path, str]) -> list[tuple[pathlib.Path, int, str]]:
    """Inspect declarations and direct constructor calls for authored exceptions."""
    modules = {source_path: ast.parse(source) for (source_path, source) in sources.items()}
    aliases_by_source = {source_path: _aliases(module, source_path) for (source_path, module) in modules.items()}

    declarations = {
        aliases_by_source[source_path][statement.name]: (source_path, statement)
        for (source_path, module) in modules.items()
        for statement in module.body
        if isinstance(statement, ast.ClassDef)
    }

    exception_parameters = _exception_parameters(declarations, aliases_by_source)
    findings = []
    error_code_parents = set()
    code_arguments = {"code", "error_code"}

    for (qualified_name, (source_path, declaration)) in declarations.items():
        if declaration.name != "ErrorCode":
            continue

        bases = [_qualified_name(base, aliases_by_source[source_path]) for base in declaration.bases]

        if (
            bases == ["enum.Enum"]
            and not any(initializer is not None for (_, initializer) in _class_assignments(declaration))
            and _lowercases_member_name(_method(declaration, "_generate_next_value_"))
        ):
            error_code_parents.add(qualified_name)
        else:
            findings.append((source_path, declaration.lineno, "ErrorCode must be a memberless plain Enum with lowercase auto() values"))

    owned_codes = {}

    for (qualified_name, (positional, keyword_only)) in exception_parameters.items():
        (source_path, declaration) = declarations[qualified_name]
        own_parameters = _constructor_parameters(declaration)
        code_enum = _nested_code(declaration)
        class_codes = [(target, initializer) for (target, initializer) in _class_assignments(declaration) if target.id in code_arguments]
        accepts_code = any(parameter.arg in code_arguments for parameter in (*positional, *keyword_only))

        if own_parameters is not None and (len(positional) > 1 or any(parameter.arg in code_arguments for parameter in positional)):
            constructor = _method(declaration, "__init__")

            findings.append((source_path, constructor.lineno, "custom exception constructors take at most one positional message"))

        if code_enum is None:
            if accepts_code or class_codes:
                findings.append((source_path, declaration.lineno, "coded exceptions must declare their own nested Code(ErrorCode)"))

            continue

        aliases = aliases_by_source[source_path]
        bases = [_qualified_name(base, aliases) for base in code_enum.bases]

        if len(bases) != 1 or bases[0] not in error_code_parents:
            findings.append((source_path, code_enum.lineno, "nested Code must derive from the shared ErrorCode"))

        if _method(code_enum, "_generate_next_value_") is not None:
            findings.append((source_path, code_enum.lineno, "nested Code must inherit ErrorCode's auto() behavior"))

        members = [(target, initializer) for (target, initializer) in _class_assignments(code_enum) if initializer is not None]
        owned_codes[qualified_name] = {f"{qualified_name}.Code.{target.id}" for (target, _) in members}

        for (target, initializer) in members:
            if not isinstance(initializer, ast.Call) or _qualified_name(initializer.func, aliases) != "enum.auto":
                findings.append((source_path, target.lineno, "declare each error code with auto()"))

        for (target, initializer) in class_codes:
            if initializer is None:
                continue

            class_aliases = aliases | {"Code": f"{qualified_name}.Code"}

            if _qualified_name(initializer, class_aliases) not in owned_codes[qualified_name]:
                findings.append((source_path, target.lineno, "store a member of this exception's own Code enum"))

        if own_parameters is not None:
            class_aliases = aliases | {"Code": f"{qualified_name}.Code"}

            for parameter in (*positional, *keyword_only):
                if parameter.arg not in code_arguments or parameter.annotation is None:
                    continue

                annotation = parameter.annotation

                if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                    annotation_name = annotation.value
                    own_annotations = {"Code", f"{declaration.name}.Code", f"{qualified_name}.Code"}
                else:
                    annotation_name = _qualified_name(annotation, class_aliases)
                    own_annotations = {f"{qualified_name}.Code"}

                if annotation_name not in own_annotations:
                    findings.append((source_path, parameter.lineno, "annotate code with this exception's nested Code"))

    for (source_path, module) in modules.items():
        aliases = aliases_by_source[source_path]

        for call in ast.walk(module):
            if not isinstance(call, ast.Call):
                continue

            qualified_name = _qualified_name(call.func, aliases)

            if qualified_name not in exception_parameters:
                continue

            (positional, _) = exception_parameters[qualified_name]
            positional_codes = [argument for (parameter, argument) in zip(positional, call.args) if parameter.arg in code_arguments]

            if len(call.args) > 1 or positional_codes:
                findings.append((source_path, call.lineno, "pass custom exception context by keyword after the message"))

            supplied_codes = [keyword.value for keyword in call.keywords if keyword.arg in code_arguments]

            supplied_codes.extend(positional_codes)

            for supplied_code in supplied_codes:
                candidates = owned_codes.get(qualified_name)

                if candidates is None or _qualified_name(supplied_code, aliases) not in candidates:
                    findings.append((source_path, supplied_code.lineno, "pass a member of this exception's own Code enum"))

    return sorted(findings)


class ErrorCodeEnumPostureTests(unittest.TestCase):
    def test_accepts_nested_enum_codes_and_builtin_exception_signatures(self):
        source = """from enum import Enum, auto

class ErrorCode(Enum):
    @staticmethod
    def _generate_next_value_(name, _start, _count, _last_values):
        return name.lower()

class Rejected(ValueError):
    class Code(ErrorCode):
        INVALID_INPUT = auto()

    def __init__(self, message, *, code: "Code"):
        super().__init__(message)
        self.code = code

raise Rejected("Explain the failure", code=Rejected.Code.INVALID_INPUT)
raise ValueError("Explain the failure")
raise OSError(errno.EIO, "Cannot read", filename)
raise ExceptionGroup("work and cleanup failed", errors)
record(code="wire field")
"""

        self.assertEqual(first=[], second=error_code_findings(sources={pathlib.Path("sample.py"): source}))

    def test_reports_positional_codes_and_other_positional_context(self):
        source_path = pathlib.Path("sample.py")

        source = """class Rejected(ValueError):
    def __init__(self, code, message):
        super().__init__(message)

class SourceLimitError(ValueError):
    def __init__(self, message, source):
        super().__init__(message)

raise Rejected("invalid_input", "Explain the failure")
raise SourceLimitError("Source exceeds its limit", source)
"""

        self.assertEqual(
            first=[
                (source_path, 1, "coded exceptions must declare their own nested Code(ErrorCode)"),
                (source_path, 2, "custom exception constructors take at most one positional message"),
                (source_path, 6, "custom exception constructors take at most one positional message"),
                (source_path, 9, "pass a member of this exception's own Code enum"),
                (source_path, 9, "pass custom exception context by keyword after the message"),
                (source_path, 10, "pass custom exception context by keyword after the message"),
            ],
            second=error_code_findings(sources={source_path: source}),
        )

    def test_reports_external_enums_strings_and_wire_values_from_imported_exceptions(self):
        declarations = """from enum import Enum, auto

class ErrorCode(Enum):
    @staticmethod
    def _generate_next_value_(name, _start, _count, _last_values):
        return name.lower()

class Rejected(ValueError):
    class Code(ErrorCode):
        INVALID_INPUT = auto()

    def __init__(self, message, *, code: Code):
        super().__init__(message)

class OtherError(Rejected):
    class Code(ErrorCode):
        INVALID_INPUT = auto()
"""

        source_path = pathlib.Path("consumer.py")

        consumer = """from failures import Rejected as Refusal, OtherError
INVALID_INPUT = "invalid_input"
raise Refusal("Explain the failure", code=Refusal.Code.INVALID_INPUT)
raise Refusal("Explain the failure", code="invalid_input")
raise Refusal("Explain the failure", code=INVALID_INPUT)
raise Refusal("Explain the failure", code=Refusal.Code.INVALID_INPUT.value)
raise Refusal("Explain the failure", code=OtherError.Code.INVALID_INPUT)
raise OtherError("Explain the failure", code=OtherError.Code.INVALID_INPUT)
"""

        self.assertEqual(
            first=[(source_path, line, "pass a member of this exception's own Code enum") for line in (4, 5, 6, 7)],
            second=error_code_findings(sources={pathlib.Path("failures.py"): declarations, source_path: consumer}),
        )

    def test_reports_external_code_bases_and_manually_spelled_members(self):
        source_path = pathlib.Path("sample.py")

        source = """from enum import Enum, StrEnum, auto

class ErrorCode(StrEnum):
    pass

class ExternalCode(Enum):
    INVALID_INPUT = auto()

class Rejected(ValueError):
    class Code(ExternalCode):
        INVALID_INPUT = "invalid_input"

    code = ExternalCode.INVALID_INPUT

    def __init__(self, message, *, code: ExternalCode):
        super().__init__(message)
"""

        self.assertEqual(
            first=[
                (source_path, 3, "ErrorCode must be a memberless plain Enum with lowercase auto() values"),
                (source_path, 10, "nested Code must derive from the shared ErrorCode"),
                (source_path, 11, "declare each error code with auto()"),
                (source_path, 13, "store a member of this exception's own Code enum"),
                (source_path, 15, "annotate code with this exception's nested Code"),
            ],
            second=error_code_findings(sources={source_path: source}),
        )

    def test_subclass_cannot_borrow_its_parents_code_enum(self):
        source_path = pathlib.Path("sample.py")

        source = """from enum import Enum, auto

class ErrorCode(Enum):
    @staticmethod
    def _generate_next_value_(name, _start, _count, _last_values):
        return name.lower()

class Rejected(ValueError):
    class Code(ErrorCode):
        INVALID_INPUT = auto()

    def __init__(self, message, *, code: Code):
        super().__init__(message)

class ChildRejected(Rejected):
    pass

raise ChildRejected("Explain the failure", code=Rejected.Code.INVALID_INPUT)
"""

        self.assertEqual(
            first=[
                (source_path, 15, "coded exceptions must declare their own nested Code(ErrorCode)"),
                (source_path, 18, "pass a member of this exception's own Code enum"),
            ],
            second=error_code_findings(sources={source_path: source}),
        )

    def test_authored_exceptions_follow_error_code_posture(self):
        findings = error_code_findings(
            sources={
                source_path.relative_to(ROOT): source_path.read_text(encoding="utf-8")
                for source_path in python_sources(production_only=False)
            }
        )

        diagnostics = [f"{source_path}:{line}: {reason}" for (source_path, line, reason) in findings]

        self.assertEqual(first=[], second=diagnostics, msg="\n".join(diagnostics))
