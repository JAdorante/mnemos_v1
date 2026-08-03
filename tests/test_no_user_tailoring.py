"""CI gate for the general-code invariant: no user- or machine-specificity in
runtime logic.

The rule (owner's): the program stays general-purpose; ALL user/machine
specificity lives in data / models / config, never in `.py` logic. This test
operationalizes that so a regression can't silently reintroduce a hardcoded
contact name, this developer's home path, or a version-stamped app folder.

Scope — RUNTIME code only:
  * scans app/, browser_agent/, desktop_agent/
  * excludes tests/, scripts/ (not imported at runtime) and eval fixtures
    (eval_*.py / eval_tasks.py — golden data, allowed to name real people)
  * checks STRING LITERALS only, and excludes docstrings + comments (cosmetic,
    per the plan): the parse is AST-based, so comments never appear and module/
    class/function docstrings are skipped explicitly.

What's banned in a runtime string literal:
  * personal names this developer used as few-shot examples (now sourced from the
    user's own vocabulary at runtime — see app/services/vocabulary.example_terms)
  * the `'vinceo.ai's`-as-example form (the product name as a possessive/subject is
    fine; quoted as an example ENTITY it was user-tailoring)
  * this machine's home path (Users\Dell...)
  * version-stamped app folders (FL Studio 2024, ...) — those belong in the
    shipped app registry JSON (data), not in config.py logic

Run:  python -m unittest tests.test_no_user_tailoring
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_RUNTIME_PKGS = ("app", "browser_agent", "desktop_agent")

# (regex, human label). Word-boundaried so "Christmas"/"Marcel" don't false-hit.
_DENYLIST = [
    (re.compile(r"\bJustin\b"), "personal name 'Justin'"),
    (re.compile(r"\bAbby\b"), "personal name 'Abby'"),
    (re.compile(r"\bMarc\b"), "personal name 'Marc'"),
    (re.compile(r"\bChris\b"), "personal name 'Chris'"),
    (re.compile(r"\bTechCorp\b"), "example company 'TechCorp'"),
    (re.compile(r"Dell Capital"), "example org 'Dell Capital'"),
    (re.compile(r"""['"]vinceo.ai['"]"""), "'vinceo.ai's-as-example-entity form"),
    (re.compile(r"[Uu]sers[\\/]+Dell"), r"this machine's home path (Users\Dell)"),
    (re.compile(r"FL Studio\s+\d"), "version-stamped app folder (FL Studio NN)"),
]


def _is_eval_fixture(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("eval_") or name == "eval_tasks.py"


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """id()s of the Constant nodes that are module/class/function docstrings, so
    they can be excluded (docstrings are cosmetic per the invariant gate's scope)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _runtime_files():
    for pkg in _RUNTIME_PKGS:
        base = _ROOT / pkg
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or _is_eval_fixture(path):
                continue
            yield path


def _scan(path: Path) -> list[str]:
    """Return human-readable violation strings for one file (empty = clean)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_ids = _docstring_constant_ids(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids):
            for rx, label in _DENYLIST:
                if rx.search(node.value):
                    rel = path.relative_to(_ROOT)
                    snippet = node.value.strip().replace("\n", " ")[:80]
                    out.append(f"{rel}:{node.lineno}: {label} -> {snippet!r}")
    return out


class NoUserTailoringTests(unittest.TestCase):
    def test_no_hardcoded_user_or_machine_specificity(self) -> None:
        violations: list[str] = []
        for path in _runtime_files():
            violations.extend(_scan(path))
        if violations:
            msg = ("Runtime code contains hardcoded user/machine specificity "
                   "(move it to data / config / the vocabulary provider):\n  "
                   + "\n  ".join(sorted(violations)))
            self.fail(msg)

    def test_scanner_actually_sees_files(self) -> None:
        """Guard against the scan silently matching nothing (e.g. a bad path)."""
        files = list(_runtime_files())
        self.assertGreater(len(files), 10, "expected to scan the runtime packages")


if __name__ == "__main__":
    unittest.main()
