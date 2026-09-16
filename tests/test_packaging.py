"""Architectural guards."""

import ast
import pathlib

import pytest

RUNTIME = pathlib.Path(__file__).resolve().parent.parent / "rainalert"


def test_runtime_never_imports_wradlib():
    """wradlib is the test oracle, not a runtime dependency (D-21).

    It drags in a large scientific stack that would slow every cold start of a job that runs 288
    times a day, and the whole point of the hand-written decoder is to avoid it.
    """
    offenders = []
    for path in RUNTIME.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "wradlib" for name in names):
                offenders.append(f"{path.relative_to(RUNTIME).as_posix()}:{node.lineno}")
    # Mentioning wradlib in a docstring is fine and in fact wanted; importing it is not.
    assert offenders == []


def test_multi_cycle_archive_is_rejected_by_read_cycle(outage_cycles):
    """Merging cycles yields duplicate leads - a plausible-looking wrong answer."""
    from rainalert.radar.decoder import RVFormatError, read_cycle

    with pytest.raises(RVFormatError, match="4 cycles"):
        read_cycle(outage_cycles)
