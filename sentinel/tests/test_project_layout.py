"""The layout the tests import through must be the layout the app runs on.

This suite exists because of a real failure. Every source root in this repo
is a separate Python import root (shared/ -> sentinel_core, backend/ -> app,
and so on), and for a while the only things that knew that were pytest.ini's
`pythonpath`, one PYTHONPATH assignment in the Makefile, and the Dockerfiles.

The result: 229 tests passed and `docker compose up` worked, while a plain
`python -m app.main` in a terminal died with

    ModuleNotFoundError: No module named 'sentinel_core'

A green suite proved nothing about whether the application starts, because
the suite reached the code by a route the application never takes. These
tests close that gap -- they assert the packaging declares the same roots
pytest does, and that the API has an entrypoint that actually serves.
"""
from __future__ import annotations

import ast
import configparser
import pathlib
import subprocess
import sys
import tomllib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
PYTEST_INI = REPO / "pytest.ini"


def _packaging_roots() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    return data["tool"]["setuptools"]["packages"]["find"]["where"]


def _pytest_roots() -> list[str]:
    cp = configparser.ConfigParser()
    cp.read(PYTEST_INI)
    return cp["pytest"]["pythonpath"].split()


def _packages_under(root: pathlib.Path) -> list[str]:
    """Top-level importable packages directly inside `root`."""
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / "__init__.py").exists())


def test_every_source_root_the_tests_use_is_also_packaged():
    """pytest.ini and pyproject.toml must not drift apart.

    A root that holds a package and is on the test path but missing from the
    packaging is precisely the bug this file documents: importable under
    pytest, absent at runtime.
    """
    packaged = set(_packaging_roots())
    missing = {}
    for root in _pytest_roots():
        pkgs = _packages_under(REPO / root)
        # Roots holding only loose scripts (database/seeds) are run as files,
        # never imported as packages, so they are legitimately not packaged.
        if pkgs and root not in packaged:
            missing[root] = pkgs

    assert not missing, (
        "these roots are importable in tests but are not installed by "
        f"`pip install -e .`, so the application cannot import them: {missing}. "
        "Add them to [tool.setuptools.packages.find] where in pyproject.toml.")


def test_every_packaged_root_actually_contains_packages():
    """Guard the other direction: a `where` entry that finds nothing is a typo.

    Silently finding zero packages is how a root gets dropped from an install
    without anybody noticing until runtime.
    """
    empty = [root for root in _packaging_roots()
             if not _packages_under(REPO / root)]
    assert not empty, (
        f"these packaging roots contain no importable package: {empty}")


@pytest.mark.parametrize("module", [
    "sentinel_core.config",
    "app.main",
    "sentinel_ai.pipeline",
    "ingestion.live_reader",
    "processor.matcher",
])
def test_each_service_imports_without_the_test_harness(module):
    """Import in a clean subprocess, from a directory that is not the repo.

    Run in-process this would prove nothing: conftest.py has already pushed
    every source root onto sys.path, so the import would succeed even with
    the packaging broken. A child interpreter started elsewhere, with the
    inherited path stripped, is the only honest check that the *installed*
    project resolves -- which is what uvicorn, VS Code and a bare shell do.
    """
    proc = subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        cwd=pathlib.Path(sys.prefix), capture_output=True, text=True,
        timeout=120)
    assert proc.returncode == 0, (
        f"`import {module}` fails outside the test harness -- the app cannot "
        f"start.\nRun `pip install -e .` from {REPO}.\n{proc.stderr.strip()}")


def test_the_api_has_an_entrypoint_that_serves():
    """`python -m app.main` must start a server, not import and exit 0.

    `app` is an ASGI object, so a module with no __main__ guard imports
    cleanly, listens on nothing, and exits successfully -- a silent no-op
    indistinguishable from success. Both sibling services have a guard; the
    API did not.
    """
    src = (REPO / "backend" / "app" / "main.py").read_text()
    tree = ast.parse(src)

    functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "main" in functions, "app/main.py defines no main() to run"

    guarded = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in tree.body)
    assert guarded, (
        "app/main.py has no `if __name__ == \"__main__\"` guard, so "
        "`python -m app.main` imports the module and exits without serving")


def test_the_declared_console_script_resolves():
    """`sentinel-api` must point at something that exists.

    A console script naming a missing function installs happily and fails
    only when a user types the command.
    """
    data = tomllib.loads(PYPROJECT.read_text())
    target = data["project"]["scripts"]["sentinel-api"]
    module_name, _, attr = target.partition(":")

    import importlib
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attr, None)), (
        f"pyproject declares sentinel-api = {target!r} but {attr}() is not "
        f"callable in {module_name}")
