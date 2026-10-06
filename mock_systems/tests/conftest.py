import pytest
from fastapi.testclient import TestClient

from crm.main import create_app as create_crm_app
from support.main import create_app as create_support_app

CRM_KEY = "test-crm-key"
SUPPORT_TOKEN = "test-support-token"


@pytest.fixture
def crm() -> TestClient:
    return TestClient(create_crm_app(api_key=CRM_KEY), headers={"X-API-Key": CRM_KEY})


@pytest.fixture
def support() -> TestClient:
    return TestClient(
        create_support_app(token=SUPPORT_TOKEN),
        headers={"Authorization": f"Bearer {SUPPORT_TOKEN}"},
    )
