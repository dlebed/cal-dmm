"""Guard the packaging metadata in ``pyproject.toml``.

Two things bite silently when new console apps or spec-data files are added
without a matching packaging change:

* a ``[project.scripts]`` entry point whose target module or callable does
  not actually exist -- ``pip install`` still succeeds; the break only shows
  up when someone runs the installed console script.
* a new ``*.yaml`` file dropped into ``src/caldmm/cal/data/`` without also
  being covered by ``[tool.setuptools.package-data]`` -- an editable install
  (what the test suite runs against) sees it via the source tree regardless,
  but a built/non-editable install silently ships without it, and the
  spec-driven pass/fail verdicts either raise on load or quietly miss models.

Both checks parse ``pyproject.toml`` itself (via stdlib ``tomllib``) rather
than hardcoding the entry-point list or the yaml file names, so the test
tracks the file instead of drifting from it.
"""

from __future__ import annotations

import importlib
import pathlib
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with open(PYPROJECT_PATH, "rb") as handle:
        return tomllib.load(handle)


def _script_entry_points() -> list[tuple[str, str]]:
    """Return ``(script_name, "module:callable")`` pairs from pyproject.toml."""
    data = _load_pyproject()
    scripts = data["project"]["scripts"]
    return sorted(scripts.items())


@pytest.mark.parametrize("script_name, target", _script_entry_points())
def test_entry_point_resolves(script_name, target):
    """Every ``[project.scripts]`` entry must import and expose the callable.

    This is exactly what ``pip install`` wires up as the console script's
    body, so a typo'd module path or a renamed function is otherwise
    invisible until someone actually runs ``caldmm-<name>``.

    Some apps (e.g. the PySide6 GUI) depend on an optional extra
    (``[project.optional-dependencies]``) that this environment may not have
    installed -- CLAUDE.md is explicit that the suite must stay runnable
    with no interface libraries installed. If the module import fails
    because a *third-party* package is missing, skip rather than fail; if
    the failure is inside ``caldmm`` itself (bad module path, typo'd
    submodule), that is a real packaging bug and must fail.
    """
    module_path, _, attr = target.partition(":")
    assert module_path and attr, (
        f"{script_name!r} entry point {target!r} is not 'module:callable'"
    )

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing_top_level = (exc.name or "").split(".")[0]
        if missing_top_level and missing_top_level != "caldmm":
            pytest.skip(
                f"{script_name!r} ({target}) needs optional dependency "
                f"{missing_top_level!r}, not installed in this environment"
            )
        raise

    assert hasattr(module, attr), (
        f"{script_name!r} points at {target!r}, but {module_path!r} has no "
        f"attribute {attr!r}"
    )
    assert callable(getattr(module, attr)), (
        f"{script_name!r} points at {target!r}, but {attr!r} is not callable"
    )


def test_cal_data_package_data_covers_every_yaml_on_disk():
    """``package-data`` must list every ``*.yaml`` under ``cal/data/``.

    Reads the on-disk directory as ground truth and cross-checks it against
    both ``[tool.setuptools.package-data]`` (what a non-editable install
    would actually ship) and what ``importlib.resources`` sees at runtime
    (what the loader in ``cal/specs.py`` actually reads). A new spec file
    added without touching ``pyproject.toml`` fails this test immediately,
    rather than shipping silently broken.
    """
    data_dir = REPO_ROOT / "src" / "caldmm" / "cal" / "data"
    assert data_dir.is_dir(), f"expected {data_dir} to exist"
    assert (data_dir / "__init__.py").exists(), (
        "caldmm.cal.data must be a real package (with __init__.py) or "
        "setuptools will not discover it as an installable package at all, "
        "regardless of package-data"
    )

    on_disk = {p.name for p in data_dir.glob("*.yaml")}
    assert on_disk, "expected at least one *.yaml spec file on disk"

    pyproject = _load_pyproject()
    package_data = pyproject["tool"]["setuptools"]["package-data"]
    patterns = package_data.get("caldmm.cal.data", [])
    assert "*.yaml" in patterns, (
        "[tool.setuptools.package-data] must map 'caldmm.cal.data' to a "
        "pattern covering '*.yaml', or a non-editable install will not ship "
        "the spec tables"
    )

    from importlib import resources

    resource_root = resources.files("caldmm.cal.data")
    seen_via_resources = {
        entry.name
        for entry in resource_root.iterdir()
        if entry.name.endswith(".yaml")
    }
    assert seen_via_resources == on_disk, (
        "importlib.resources.files('caldmm.cal.data') does not see the "
        f"same *.yaml files as are on disk: resources={seen_via_resources!r} "
        f"disk={on_disk!r}"
    )
