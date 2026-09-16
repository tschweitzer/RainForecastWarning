from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def wet_cycle() -> Path:
    """2026-09-16 13:55 UTC - a rainy cycle, frames _000/_060/_120."""
    return FIXTURES / "DE1200_RV2609161355_trimmed.tar.bz2"


@pytest.fixture(scope="session")
def outage_cycles() -> Path:
    """2026-09-15 16:15-16:30 - the Borkum radar dropout, frames _000/_005/_060 of four cycles."""
    return FIXTURES / "DE1200_RV_outage_20260915_1615-1630.tar.bz2"
