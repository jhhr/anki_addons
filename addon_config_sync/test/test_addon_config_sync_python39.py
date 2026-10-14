"""The add-on imports on Python 3.9, which Anki ran on until 26.05.

`X | None` in an annotation that Python evaluates when the module is imported is a
TypeError before 3.10. The add-on's own Anki runs on 3.13 and so does this suite, so nothing
else would notice. Annotations are evaluated in signatures and at module and class level,
not inside function bodies, and not at all under `from __future__ import annotations`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1]


def _evaluated_annotations(tree: ast.Module) -> list[ast.expr]:
    found: list[ast.expr] = []

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.AnnAssign):
                found.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = node.args
                for argument in (
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                    arguments.vararg,
                    arguments.kwarg,
                ):
                    if argument is not None and argument.annotation is not None:
                        found.append(argument.annotation)
                if node.returns is not None:
                    found.append(node.returns)
            elif isinstance(node, ast.ClassDef):
                visit(node.body)
            elif isinstance(node, (ast.If, ast.Try)):
                visit(node.body)
                visit(node.orelse)

    visit(tree.body)
    return found


def _defers_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


@pytest.mark.parametrize("module", sorted(path.name for path in ADDON.glob("*.py")))
def test_no_annotation_python_39_would_evaluate_has_a_union(module):
    tree = ast.parse((ADDON / module).read_text(encoding="utf-8"))
    if _defers_annotations(tree):
        return
    unions = [
        ast.unparse(annotation)
        for annotation in _evaluated_annotations(tree)
        if any(
            isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
            for node in ast.walk(annotation)
        )
    ]
    assert unions == [], f"{module} needs `from __future__ import annotations`"
