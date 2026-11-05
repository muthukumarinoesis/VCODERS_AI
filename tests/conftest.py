import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from pipeline_router import SafetyCaps  # noqa: E402


@pytest.fixture
def caps():
    return SafetyCaps()


@pytest.fixture
def pipelines_dir():
    return ROOT / "examples" / "pipelines"


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    import svc

    while svc.ENGINES:
        svc.ENGINES.pop().dispose()
