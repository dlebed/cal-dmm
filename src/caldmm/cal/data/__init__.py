"""Shipped per-model specification files (``*.yaml``).

A package rather than a bare directory so the files can be reached with
``importlib.resources.files('caldmm.cal.data')`` instead of ``__file__``
arithmetic, and so ``setuptools`` discovers the directory at all.

Note that ``[tool.setuptools.package-data]`` in ``pyproject.toml`` still has to
list ``*.yaml`` or the specification files will not be installed.
"""
