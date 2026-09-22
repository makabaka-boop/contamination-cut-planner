import sys
from pathlib import Path

import pytest

# Make the repository root importable when pytest is run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.repository import InMemoryRepository  # noqa: E402


@pytest.fixture
def repo():
    return InMemoryRepository()


@pytest.fixture
def client(repo):
    app = create_app(repository=repo)
    with TestClient(app) as c:
        c.app.state.repo_owned = False
        yield c
