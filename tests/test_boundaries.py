"""Architectural boundaries, asserted rather than documented.

Three seams carry the project's claims. Each one is a rule that a well-meaning contributor
would break without noticing, and each break would be invisible until it invalidated a
result. So they are tests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _python_files(package: str) -> list[Path]:
    return sorted((ROOT / package).rglob("*.py"))


def test_only_the_llm_client_imports_the_provider_sdk():
    """All model access goes through one LLMClient protocol with a single method.

    Swapping or adding a provider must be one class. The moment a stage imports `openai`
    directly, the "generalisation seam" claim in V1_SCOPE becomes false, and it becomes
    false silently.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files("sherlog")
        if path.name != "client.py"
        and any(m.split(".")[0] == "openai" for m in _imports(path))
    ]
    assert offenders == [], f"these modules import the provider SDK directly: {offenders}"


def test_sherlog_never_imports_from_the_target_system():
    """sherLOG reads Loki, Prometheus, Tempo and Postgres — never the app's own code.

    If the engine imported nimbus_common it would be diagnosing a system it is coupled to,
    and pointing v2 at a different system would stop being a config change. This is the
    boundary that makes "generalisable" mean something.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files("sherlog")
        if any(m.split(".")[0] in {"nimbus", "nimbus_common"} for m in _imports(path))
    ]
    assert offenders == [], f"the engine imports target-system code: {offenders}"


def test_the_target_system_never_imports_the_engine():
    """The reverse direction matters too. A service that imported sherlog could leak
    engine state into application telemetry, which the engine would then read back as
    evidence."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files("nimbus")
        if any(m.split(".")[0] in {"sherlog", "faults"} for m in _imports(path))
    ]
    assert offenders == [], f"the target system imports the engine: {offenders}"


def test_the_engine_never_imports_the_fault_catalog():
    """Ground truth isolation at the import level.

    The database permissions in 02_ground_truth_isolation.sql are the real enforcement, but
    an `from faults.schema import ...` inside the engine would be a second, quieter path to
    the answers — and one no DBA would catch.
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files("sherlog")
        if any(m.split(".")[0] == "faults" for m in _imports(path))
    ]
    assert offenders == [], f"the engine imports the fault catalog: {offenders}"


@pytest.mark.parametrize("package", ["sherlog", "faults", "nimbus"])
def test_every_module_parses(package):
    """Cheap smoke test. A syntax error in a service that only runs in Docker would
    otherwise surface at deploy time on a box we have to SSH into."""
    for path in _python_files(package):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
