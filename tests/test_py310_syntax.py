"""Every of135i/*.py (and tools/*.py) must parse on Python 3.10.

pyproject says requires-python >= 3.10 but CI runs one interpreter, and
ast.parse(feature_version=...) does not reject the 3.12 f-string
relaxations. So: parse with the running interpreter, then walk each
f-string replacement field and refuse what 3.10/3.11 cannot parse -- a
backslash, or the enclosing quote character, inside the field.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def py310_problems(path: str) -> list[str]:
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path, feature_version=(3, 10))
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        outer = ast.get_source_segment(src, node) or ""
        body = outer.lstrip("rRfF")
        quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
        for part in node.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            seg = ast.get_source_segment(src, part.value) or ""
            if "\\" in seg:
                problems.append(f"{path}:{part.value.lineno}: backslash in f-string field")
            elif len(quote) == 1 and quote in seg:
                problems.append(f"{path}:{part.value.lineno}: reused quote in f-string field")
    return problems


def sources() -> list[str]:
    out = []
    for d in ("of135i", "tools"):
        base = os.path.join(ROOT, d)
        out += [os.path.join(base, f) for f in sorted(os.listdir(base)) if f.endswith(".py")]
    return out


class Py310Syntax(unittest.TestCase):
    def test_no_py312_only_fstrings(self):
        problems = [p for f in sources() for p in py310_problems(f)]
        self.assertEqual(problems, [])

    def test_checker_catches_the_known_offender(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.py")
            open(p, "w").write("x = 1\ny = f\"{'a\\'b' if x else 'c'}\"\n")
            self.assertTrue(py310_problems(p))


if __name__ == "__main__":
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Py310Syntax))
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} tests passed")
    sys.exit(0 if result.wasSuccessful() else 1)
