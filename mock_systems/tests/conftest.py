import pytest
from fastapi.testclient import TestClient

from crm.main import create_app as create_crm_app

CRM_KEY = "test-crm-key"


@pytest.fixture
def crm() -> TestClient:
    return TestClient(create_crm_app(api_key=CRM_KEY), headers={"X-API-Key": CRM_KEY})
