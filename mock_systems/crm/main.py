import os
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader

from common.faults import install_faults
from crm.models import Customer, CustomerCreate, CustomerPage, CustomerPatch
from crm.store import CustomerStore

DEFAULT_API_KEY = "crm-dev-key"
V2_RENAMES = {"phone": "phone_number"}
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="CRM API key.")


def create_app(api_key: str | None = None, admin_token: str | None = None) -> FastAPI:
    expected = api_key or os.environ.get("CRM_API_KEY", DEFAULT_API_KEY)
    store = CustomerStore()

    def require_api_key(provided: Annotated[str | None, Security(_api_key_header)]) -> None:
        if provided is None or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")

    app = FastAPI(
        title="Mock CRM",
        version="1.0.0",
        description="Mock enterprise CRM. snake_case fields, string customer ids, E.164 phones.",
        dependencies=[Depends(require_api_key)],
        responses={
            400: {"description": "Request body is not parseable JSON."},
            401: {"description": "Missing or invalid X-API-Key header."},
        },
    )
    app.state.store = store

    @app.get("/customers", response_model=CustomerPage, summary="List customers")
    def list_customers(
        page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
        page_size: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
    ) -> CustomerPage:
        items, total = store.list(page, page_size)
        return CustomerPage(items=items, page=page, page_size=page_size, total=total)

    @app.get(
        "/customers/{customer_id}",
        response_model=Customer,
        summary="Get a customer",
        responses={404: {"description": "Customer not found."}},
    )
    def get_customer(customer_id: str) -> Customer:
        customer = store.get(customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        return customer

    @app.post("/customers", response_model=Customer, status_code=201, summary="Create a customer")
    def create_customer(body: CustomerCreate) -> Customer:
        return store.create(body)

    @app.patch(
        "/customers/{customer_id}",
        response_model=Customer,
        summary="Partially update a customer",
        responses={404: {"description": "Customer not found."}},
    )
    def patch_customer(customer_id: str, body: CustomerPatch) -> Customer:
        customer = store.patch(customer_id, body)
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found.")
        return customer

    install_faults(
        app,
        admin_token=admin_token,
        renames=V2_RENAMES,
        reset_state=store.reset,
        error_body=lambda _status, message: {"detail": message},
    )
    return app


app = create_app()
