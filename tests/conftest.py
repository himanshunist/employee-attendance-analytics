import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ["MONGO_DB"] = "attendance_test"

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client():
    main.client.drop_database(main.db.name)
    main._indexes_ready = False
    with TestClient(main.app) as test_client:
        test_client.get("/health")
        yield test_client
    main.client.drop_database(main.db.name)
