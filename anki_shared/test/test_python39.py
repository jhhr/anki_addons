"""Every addon here loads and runs on Python 3.9.

Anki's own builds ran 3.9 from 2.1.50 until 25.07, and no addon here asks for a newer Anki. A
module that 3.9 cannot import stops the whole addon loading. The developer's Anki and this suite
run a newer Python, and mypy 2 cannot check against 3.9, so nothing else notices when code
written there stops working on 3.9.

This reads every module `build.py dist` packages (`package_entries`), the shared packages and
the jp_text_processing submodule included, for the ways that has happened or easily could:

- syntax 3.9 does not have: `match`, `except*` and type parameters (what the parser's
  `feature_version` rejects), `a[*b]`, and an f-string whose replacement field holds the
  f-string's own quote, a backslash or a comment;
- `X | None` in an annotation that Python evaluates, in a module without
  `from __future__ import annotations`: at module and class level, and in every signature;
- a union evaluated outside any annotation, such as an alias or an `isinstance` argument;
- the newer names, modules and keyword arguments listed below, unless they sit under
  `TYPE_CHECKING`, a `sys.version_info` test, or a `try` that catches their absence.

A newer stdlib function that is not on the lists gets through. Once, mypy 1.19 at
`--python-version 3.9` checked the whole repo for those, but the mypy pinned here cannot do
that any more.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Importing one of these from `typing` is an ImportError on 3.9
NEWER_TYPING = frozenset({
    # 3.10
    "Concatenate",
    "ParamSpec",
    "ParamSpecArgs",
    "ParamSpecKwargs",
    "TypeAlias",
    "TypeGuard",
    "is_typeddict",
    # 3.11
    "LiteralString",
    "Never",
    "NotRequired",
    "Required",
    "Self",
    "TypeVarTuple",
    "Unpack",
    "assert_never",
    "assert_type",
    "clear_overloads",
    "dataclass_transform",
    "get_overloads",
    "reveal_type",
    # 3.12 and 3.13
    "TypeAliasType",
    "override",
    "NoDefault",
    "ReadOnly",
    "TypeIs",
})
# Other names from after 3.9 that code written on a newer Python reaches for
NEWER_NAMES = {
    "asyncio": {"Runner", "TaskGroup", "timeout", "timeout_at"},
    "contextlib": {"aclosing", "chdir"},
    "datetime": {"UTC"},
    "enum": {"StrEnum", "member", "nonmember", "verify"},
    "hashlib": {"file_digest"},
    "inspect": {"get_annotations"},
    "itertools": {"batched", "pairwise"},
    "logging": {"getLevelNamesMapping"},
    "math": {"cbrt", "exp2"},
    "operator": {"call"},
    "statistics": {"correlation", "covariance", "linear_regression"},
    "sys": {"orig_argv", "stdlib_module_names"},
    "types": {"EllipsisType", "NoneType", "NotImplementedType", "UnionType"},
}
NEWER_MODULES = frozenset({"tomllib"})
NEWER_BUILTINS = frozenset(
    {"BaseExceptionGroup", "EncodingWarning", "ExceptionGroup", "aiter", "anext"}
)
# Keyword arguments that 3.9 rejects with a TypeError when the call runs
NEWER_KEYWORDS = {
    "zip": {"strict"},
    "dataclass": {"kw_only", "match_args", "slots"},
    **{name: {"key"} for name in ("bisect", "bisect_left", "bisect_right")},
    **{name: {"key"} for name in ("insort", "insort_left", "insort_right")},
}
# A `|` with one of these on either side joins types, which 3.9 cannot do outside a deferred
# annotation. A union of two classes of the addon's own is not seen: a `|` between two plain
# names is just as often a set union or a Qt flag.
TYPE_NAMES = frozenset(
    {"bool", "bytearray", "bytes", "complex", "dict", "float", "frozenset", "int", "list"}
    | {"object", "set", "str", "tuple", "type"}
)
GENERIC_NAMES = TYPE_NAMES | {
    "Annotated",
    "AsyncIterable",
    "AsyncIterator",
    "Awaitable",
    "Callable",
    "ClassVar",
    "Collection",
    "Coroutine",
    "Dict",
    "Final",
    "FrozenSet",
    "Generator",
    "Iterable",
    "Iterator",
    "List",
    "Literal",
    "Mapping",
    "MutableMapping",
    "MutableSequence",
    "Optional",
    "Sequence",
    "Set",
    "Tuple",
    "Type",
    "Union",
}
# A `try` whose handler catches one of these is how code falls back where a name is missing
FALLBACK_EXCEPTIONS = frozenset(
    {"AttributeError", "BaseException", "Exception", "ImportError", "ModuleNotFoundError"}
    | {"TypeError"}
)


def _load_build():
    spec = importlib.util.spec_from_file_location("_build_for_python39_test", ROOT / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _is_type(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Name):
        return node.id in TYPE_NAMES
    if isinstance(node, ast.Subscript):
        value = node.value
        if isinstance(value, ast.Name):
            return value.id in GENERIC_NAMES
        return isinstance(value, ast.Attribute) and value.attr in GENERIC_NAMES
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_type(node.left) or _is_type(node.right)
    return False


def _has_union(node: ast.expr | None) -> bool:
    return node is not None and any(
        isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr) for part in ast.walk(node)
    )


def _tests_version_or_type_checking(test: ast.expr) -> bool:
    return any(
        (isinstance(node, ast.Name) and node.id in ("TYPE_CHECKING", "version_info"))
        or (isinstance(node, ast.Attribute) and node.attr in ("TYPE_CHECKING", "version_info"))
        for node in ast.walk(test)
    )


def _catches_absence(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(name, ast.Name) and name.id in FALLBACK_EXCEPTIONS for name in caught)


class _Checker(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.problems: list[tuple[int, str]] = []
        self.defers = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(alias.name == "annotations" for alias in node.names)
            for node in tree.body
        )
        # What each name a module is imported as stands for, `import itertools as it` included
        self.modules = {
            alias.asname or alias.name: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.bound.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.bound.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self.bound.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.arg):
                self.bound.add(node.arg)
        self.functions = 0
        self.guarded = 0

    def report(self, node: ast.AST, what: str) -> None:
        if not self.guarded:
            self.problems.append((getattr(node, "lineno", 0), what))

    def annotation(self, node: ast.expr | None, evaluated: bool) -> None:
        if evaluated and _has_union(node):
            assert node is not None
            self.report(
                node,
                f"`{ast.unparse(node)}` is evaluated, and 3.9 cannot: the module needs"
                " `from __future__ import annotations`",
            )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # A local variable's annotation is never evaluated; a module's or a class's is
        self.annotation(node.annotation, not self.defers and not self.functions)
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        arguments = node.args
        for default in (*arguments.defaults, *arguments.kw_defaults):
            if default is not None:
                self.visit(default)
        # A signature's annotations are evaluated where the def runs, a nested one's included
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        ):
            if argument is not None:
                self.annotation(argument.annotation, not self.defers)
        self.annotation(node.returns, not self.defers)
        self.functions += 1
        for statement in node.body:
            self.visit(statement)
        self.functions -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        guard = _tests_version_or_type_checking(node.test)
        self.guarded += guard
        self.generic_visit(node)
        self.guarded -= guard

    def visit_Try(self, node: ast.Try) -> None:
        guard = any(_catches_absence(handler) for handler in node.handlers)
        self.guarded += guard
        for statement in node.body:
            self.visit(statement)
        self.guarded -= guard
        for part in (*node.handlers, *node.orelse, *node.finalbody):
            self.visit(part)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitOr) and (_is_type(node.left) or _is_type(node.right)):
            self.report(node, f"`{ast.unparse(node)}` joins types, which 3.9 cannot do here")
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        newer = NEWER_TYPING if module == "typing" else NEWER_NAMES.get(module, set())
        for alias in node.names:
            if alias.name in newer:
                self.report(node, f"{module}.{alias.name} is newer than 3.9")
        if module in NEWER_MODULES:
            self.report(node, f"{module} is newer than 3.9")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in NEWER_MODULES:
                self.report(node, f"{alias.name} is newer than 3.9")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            module = self.modules.get(node.value.id, "")
            newer = NEWER_TYPING if module == "typing" else NEWER_NAMES.get(module, set())
            if node.attr in newer:
                self.report(node, f"{module}.{node.attr} is newer than 3.9")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and node.id in NEWER_BUILTINS
            and node.id not in self.bound
        ):
            self.report(node, f"{node.id} is newer than 3.9")

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr if isinstance(function, ast.Attribute) else ""
        )
        newer = NEWER_KEYWORDS.get(name, set()) & {k.arg for k in node.keywords if k.arg}
        if newer:
            self.report(node, f"{name}() takes no {', '.join(sorted(newer))} before 3.10")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.slice, ast.Tuple) and any(
            isinstance(element, ast.Starred) for element in node.slice.elts
        ):
            self.report(node, "a starred index (`a[*b]`) needs 3.11")
        self.generic_visit(node)


def _newer_fstrings(source: str) -> list[tuple[int, str]]:
    """Replacement fields that only 3.12 can parse. Before 3.12 an f-string was first read as
    one string literal, so its fields could hold neither the quote that ends it, nor a
    backslash, nor a comment, nor (in a single-quoted one) a line break. The tokens that show
    a field's insides exist from 3.12 on; an older Python fails to parse such a field anyway."""
    if sys.version_info < (3, 12):
        return []
    found = []
    quotes: list[str] = []  # the quote that ends each f-string being read, innermost last
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        in_field = bool(quotes) and token.type not in (
            tokenize.FSTRING_MIDDLE,
            tokenize.FSTRING_END,
        )
        if in_field:
            text = token.string
            if token.type in (tokenize.STRING, tokenize.FSTRING_START) and quotes[-1] in text:
                found.append((token.start[0], "an f-string's field holds the quote that ends it"))
            elif "\\" in text or token.type == tokenize.COMMENT:
                found.append((token.start[0], "an f-string's field holds a backslash or comment"))
            elif token.type in (tokenize.NL, tokenize.NEWLINE) and len(quotes[-1]) == 1:
                found.append((token.start[0], "a single-quoted f-string's field spans lines"))
        if token.type == tokenize.FSTRING_START:
            quotes.append(token.string.lstrip("bBfFrRtTuU"))
        elif token.type == tokenize.FSTRING_END:
            quotes.pop()
    return found


def problems(source: str) -> list[tuple[int, str]]:
    """(line, what) for each thing in the module that 3.9 cannot run."""
    try:
        tree = ast.parse(source, feature_version=(3, 9))
    except SyntaxError as error:
        return [(error.lineno or 0, f"3.9 cannot parse this: {error.msg}")]
    checker = _Checker(tree)
    checker.visit(tree)
    return sorted(checker.problems + _newer_fstrings(source))


BUILD = _load_build()


@pytest.mark.parametrize("addon", BUILD.discover(), ids=lambda addon: addon.path.name)
def test_every_module_an_addon_ships_runs_on_python_39(addon):
    found = []
    for path, arc in BUILD.package_entries(addon):
        # lib/ is vendored and built for the Python Anki ships now; on an older one the addon
        # offers to rebuild it from requirements.txt, whose pins go down to 3.9
        if arc.endswith(".py") and not arc.startswith("lib/"):
            source = path.read_text(encoding="utf-8-sig")
            found.extend(f"{arc}:{line}: {what}" for line, what in problems(source))
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "source",
    [
        "match x:\n    case 1:\n        pass\n",
        "try:\n    pass\nexcept* ValueError:\n    pass\n",
        "def f[T](x: T) -> T: ...\n",
        "a[*b]\n",
        'f"{x["a"]}"\n',
        "f\"{'-'.join(x)}\" + f\"{'\\n'.join(x)}\"\n",
        'f"""{x  # why\n}"""\n',
        "def f(x: int | None) -> None: ...\n",
        "def f() -> list[str] | None: ...\n",
        "class A:\n    x: dict[str, int] | None = None\n",
        "def f():\n    def g(x: int | None): ...\n",
        "from __future__ import annotations\nAlias = int | None\n",
        "from __future__ import annotations\nisinstance(x, int | str)\n",
        "from __future__ import annotations\nT = TypeVar('T', bound=Literal['a'] | None)\n",
        "from typing import TypeAlias\n",
        "import typing\nx = typing.Self\n",
        "from itertools import pairwise\n",
        "import itertools as it\nit.pairwise(x)\n",
        "import tomllib\n",
        "x = anext(y)\n",
        "except_group = ExceptionGroup\n",
        "zip(a, b, strict=True)\n",
        "@dataclass(slots=True)\nclass A:\n    pass\n",
        "bisect.insort(a, x, key=len)\n",
    ],
)
def test_the_check_sees_what_python_39_cannot_run(source):
    assert problems(source)


@pytest.mark.parametrize(
    "source",
    [
        "from __future__ import annotations\ndef f(x: int | None) -> list[str] | None: ...\n",
        "from __future__ import annotations\nclass A:\n    x: int | None = None\n",
        "def f():\n    x: int | None = None\n",
        "def f(x: 'int | None'): ...\n",
        "if TYPE_CHECKING:\n    from typing import TypeAlias\n    Alias = int | None\n",
        "if sys.version_info >= (3, 11):\n    from typing import Self\n",
        "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None\n",
        "flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop\n",
        "both = left | right\nmerged = defaults | overrides\n",
        "def anext(it): ...\nanext(x)\n",
        "f'{x[\"a\"]}' + f\"{x['a']!r:>{width}}\"\n",
        'f"""{x["a"]}"""\n',
        "with (open(a) as f, open(b) as g):\n    pass\n",
        "x = list[int]\nisinstance(x, (int, str))\n",
        "zip(a, b)\n",
    ],
)
def test_the_check_lets_through_what_python_39_runs(source):
    assert problems(source) == []
