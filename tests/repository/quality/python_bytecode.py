"""Find missing bytecode suppression in literal first-party Python launches.

This is a literal-form check, not a shell/data-flow parser. It does not prove
computed argv/executables, shared Docker arrays, arbitrary env variables, shell
functions, conditional exports or stdin programs. Entrypoint declarations alone
are not launches. Explicit pip/venv/build/bytecode compiler operations are exempt.
"""

from tools.container_guard import require_container

require_container()

import ast
import pathlib
from dataclasses import dataclass
import re
import shlex
import sys

from .sources import is_python_source, source_files


@dataclass(frozen=True)
class Finding:
    source_path: pathlib.Path
    line: int

    def diagnostic(self):
        return f"{self.source_path}:{self.line}: put -B before the Python payload; -I/-E ignore PYTHONDONTWRITEBYTECODE."


_COMPILERS = {"pip", "venv", "ensurepip", "build", "setuptools", "compileall", "py_compile"}

_SKIP = {
    ".git",
    ".agents",
    ".codex",
    ".aws",
    "secrets",
    "tests",
    "test",
    "fixtures",
    "docs",
    "notes",
    "vendor",
    "vendored",
    "third_party",
    "bootstrap",
    "generated",
    "node_modules",
    "dependencies",
    ".venv",
    "venv",
    "__pycache__",
    ".work",
    ".cache",
    "build",
    "dist",
    "release-artifacts"
}

_PYTHON = re.compile(r"python(?:3(?:\.\d+)?)?$")
_ENV = "PYTHONDONTWRITEBYTECODE"


def production_files(root):
    return source_files(
        root=root,
        patterns=("**/*.py", "**/*.sh", "**/*.mk", "**/Makefile", "**/GNUmakefile"),
        exclude_parts=_SKIP,
    )


def _python(executable_word):
    return (type(executable_word) is str) and (_PYTHON.fullmatch(executable_word.rsplit("$(", 1)[-1].lstrip("(").rsplit("/", 1)[-1]) is not None)


def _inline_imports(source):
    try:
        tree = ast.parse(source)
    # silent-defaults: allow-exception-outcome -- Unparseable inline code cannot prove first-party Python imports.
    except SyntaxError:
        return False

    modules = {name.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names}

    modules.update(
        node.module.split(".")[0] for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    return bool(modules - sys.stdlib_module_names - _COMPILERS)


# silent-defaults: allow-contract -- Absent explicit environment control leaves the launch checker conservative; only an admitted flag proves suppression.
def _needs_flag(arguments, env_enabled=False):
    disabled = isolated = False
    index = 0

    while index < len(arguments):
        argument = arguments[index]

        if argument is None or "[@]" in argument or argument in {"$@", "$*"}:
            return False  # Unknown argv expansion cannot be classified literally.

        if argument in {"-m", "-c"}:
            if ((index + 1) == len(arguments)) or (arguments[index + 1] is None):
                return False

            payload = arguments[index + 1]

            if argument == "-m" and payload.split(".")[0] in _COMPILERS:
                return False

            if (argument == "-c") and (not _inline_imports(payload)):
                return False

            return (not disabled) and ((isolated) or (not env_enabled))

        if argument == "-":
            return False  # Heredoc/stdin programs are a documented boundary.

        if argument == "--":
            return ((index + 1) < len(arguments)) and (not disabled) and ((isolated) or (not env_enabled))

        if argument.startswith("-"):
            if argument in {"-W", "-X"}:
                index += 2

                continue

            if not re.fullmatch(r"-[BEISOPqsubvix]+", argument):
                return False  # Unknown switches are not assumed to be payload.

            disabled |= "B" in argument[1:]
            isolated |= "I" in argument[1:] or "E" in argument[1:]
        else:
            return (not disabled) and ((isolated) or (not env_enabled))

        index += 1

    return False


def _logical_lines(source):
    (pending, first) = ("", 1)

    for (number, line) in enumerate(source.splitlines(), 1):
        if not pending:
            first = number

        if line.endswith("\\"):
            pending += line[:-1] + " "
        else:
            yield (first, pending + line)
            pending = ""

    if pending:
        yield (first, pending)


def shell_findings(source_path, source):
    findings = []
    (global_env, preface) = (False, (source_path.name not in {"Makefile", "GNUmakefile"}) and (source_path.suffix != ".mk"))

    for (number, line) in _logical_lines(source):
        if preface and re.fullmatch(r"export PYTHONDONTWRITEBYTECODE=['\"]?1['\"]?", line):
            global_env = True

            continue

        if re.match(r"\s*(?:if\b|case\b|for\b|while\b|function\b|\w+\(\))", line):
            preface = False

        if re.search(r"(?:unset\s+PYTHONDONTWRITEBYTECODE|PYTHONDONTWRITEBYTECODE=(?!['\"]?1\b))", line):
            global_env = False

        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            continue  # Multiline quoted command construction needs owner review.

        if (not tokens) or (tokens[0].lstrip("@") in {"echo", "printf"}):
            continue

        for (index, token) in enumerate(tokens):
            if not _python(token.lstrip("@")):
                continue

            entrypoint = index > 0 and tokens[index - 1] == "--entrypoint"
            argument_offset = 1

            if entrypoint:
                argument_offset = 2

            arguments = tokens[index + argument_offset:]

            # An array declaration ending at the entrypoint has no program.
            if (not arguments) or (arguments[0] == ")"):
                continue

            if (
                (index > 0) and
                (not entrypoint) and
                (not arguments[0].startswith("-")) and
                (not arguments[0].rstrip(")").endswith((".py", ".pyz")))
            ):
                continue  # E.g. helper(image, python3, uid, gid), not a launch.

            prefix = tokens[:index]
            enabled = (global_env) and (not entrypoint)

            for (position, prefix_argument) in enumerate(prefix):
                if prefix_argument in {"-i", "--ignore-environment", "--unset=" + _ENV}:
                    enabled = False
                elif (prefix_argument == "-u") and ((position + 1) < len(prefix)) and (prefix[position + 1] == _ENV):
                    enabled = False
                elif prefix_argument.startswith(_ENV + "="):
                    if entrypoint and position > 0 and prefix[position - 1] in {"--env", "-e"}:
                        enabled = prefix_argument == (_ENV + "=1")
                    elif (not entrypoint) and (position == 0 or prefix[position - 1] != "--env"):
                        enabled = prefix_argument == (_ENV + "=1")

            isolated = _needs_flag(arguments, True)

            if (entrypoint) and (any("[@]" in prefix_argument for prefix_argument in prefix)) and (not isolated):
                continue  # Shared creation/env argv is not visible at this site.

            if _needs_flag(arguments, enabled):
                findings.append(Finding(source_path, number))

    return sorted(set(findings), key=lambda finding: finding.line)


def _literal_text(node):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value

    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "executable":
        return "python3"

    return None


def _env_enabled(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _env_enabled(node.right)  # Only a literal right-hand override wins.

    if isinstance(node, ast.Dict):
        for (key, setting_expression) in reversed(list(zip(node.keys, node.values))):
            if key is None:
                return False  # A later dictionary expansion may override it.

            if _literal_text(key) == _ENV:
                return _literal_text(setting_expression) == "1"

    return False


def python_findings(source_path, source):
    findings = []

    for node in ast.walk(ast.parse(source, filename=str(source_path))):
        if (not isinstance(node, ast.Call)) or (not node.args):
            continue

        function = node.func
        name = ""

        if isinstance(function, ast.Attribute):
            name = function.attr
        elif isinstance(function, ast.Name):
            name = function.id

        if name not in {"run", "Popen", "call", "check_call", "check_output"}:
            continue

        argv = node.args[0]

        if (not isinstance(argv, (ast.List, ast.Tuple))) or (not argv.elts) or (not _python(_literal_text(argv.elts[0]))):
            continue

        command_arguments = [_literal_text(argument) for argument in argv.elts[1:]]
        enabled = any(keyword.arg == "env" and _env_enabled(keyword.value) for keyword in node.keywords)

        if _needs_flag(command_arguments, enabled):
            findings.append(Finding(source_path, node.lineno))

    return findings


def findings(source_path, source):
    return python_findings(source_path, source) if is_python_source(source_path=source_path) else shell_findings(source_path, source)


def scan(root):
    return [finding for source_path in production_files(root) for finding in findings(source_path.relative_to(root), source_path.read_text())]
