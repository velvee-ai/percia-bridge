import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from fakes import FakePercia

from percia_bridge import logs


@pytest.fixture(autouse=True)
def _clean_secrets():
    logs.clear_secrets()
    yield
    logs.clear_secrets()


@pytest.fixture
def percia():
    srv = FakePercia()
    yield srv
    srv.close()
