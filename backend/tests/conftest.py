import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import seed_users
from app.database import Base, get_db
from app.main import app


def as_user(username: str) -> dict:
    return {"X-User": username}


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        seed_users(db)
    return factory


@pytest.fixture()
def client(session_factory):
    def override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def draft_invoice(client):
    """A $5,000.00 draft invoice prepared by priya."""
    h = as_user("priya")
    cust = client.post("/customers", json={"name": "Acme Auto", "email": "ap@acme.test"}, headers=h).json()
    client.post(
        "/campaigns",
        json={
            "customer_id": cust["id"],
            "name": "Fall Promo",
            "booked_impressions": 2_000_000,
            "delivered_impressions": 1_000_000,
            "cpm_cents": 500,
        },
        headers=h,
    )
    return client.post("/invoices/generate", json={"customer_id": cust["id"]}, headers=h).json()


@pytest.fixture()
def posted_invoice(client, draft_invoice):
    i = draft_invoice["id"]
    client.post(f"/invoices/{i}/actions/submit", headers=as_user("priya"))
    client.post(f"/invoices/{i}/actions/approve", headers=as_user("marcus"))
    return client.post(f"/invoices/{i}/actions/post", headers=as_user("marcus")).json()


@pytest.fixture()
def fake_netsuite():
    """A fresh mock NetSuite per test, used by both the API and direct worker calls."""
    from app.netsuite import FakeNetSuite, get_netsuite

    client = FakeNetSuite()
    app.dependency_overrides[get_netsuite] = lambda: client
    yield client
    app.dependency_overrides.pop(get_netsuite, None)


def upload(client, csv_text, filename="delivery.csv", user="priya"):
    return client.post("/delivery-reports", json={"filename": filename, "csv": csv_text}, headers=as_user(user))
