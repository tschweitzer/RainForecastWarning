"""Marks the suite as a package.

Without this file pytest puts `tests/` itself on `sys.path`, so `import tests.helpers` resolves
only when the repository root happens to be on the path too - which is true for
`python -m pytest` and false for the `pytest` console script. With it, pytest prepends the
repository root instead and every invocation imports the same way.
"""
