"""Load test:  locust -f tests/load/locustfile.py --host http://localhost:8000 -u 50 -r 10 -t 1m --headless
Requires `python -m scripts.seed`. 409 on a lost claim race is the *expected* outcome, not a failure."""
import uuid
from datetime import datetime, timedelta, timezone

from locust import HttpUser, between, task

PW = "demo-password-123"


def _login(client, email):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PW}, name="/auth/login")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class MarketplaceUser(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self):
        self.donor = _login(self.client, "donor@example.com")
        self.ngo = _login(self.client, "ngo1@example.com")
        self.ngo_org = self.client.get("/api/v1/organisations", headers=self.ngo, name="/organisations").json()[0]["id"]

    @task(10)
    def ngo_browses_feed(self):
        self.client.get("/api/v1/donations/nearby", params={"ngo_org_id": self.ngo_org}, headers=self.ngo, name="/donations/nearby")

    @task(2)
    def donor_publishes_and_ngo_claims(self):
        now = datetime.now(timezone.utc)
        body = {"title": "Load test meals", "category": "PREPARED_MEALS", "quantity": 20, "unit": "PORTIONS",
                "prepared_at": (now - timedelta(minutes=5)).isoformat(), "expires_at": (now + timedelta(hours=4)).isoformat(),
                "pickup_start": (now - timedelta(minutes=1)).isoformat(), "pickup_end": (now + timedelta(hours=2)).isoformat(),
                "location": {"latitude": 25.3176, "longitude": 82.9739}}
        r = self.client.post("/api/v1/donations", json=body, headers=self.donor, name="/donations [create]")
        if r.status_code != 201:
            return
        did = r.json()["id"]
        self.client.post(f"/api/v1/donations/{did}/publish", headers=self.donor, name="/donations/{id}/publish")
        with self.client.post(f"/api/v1/donations/{did}/claim", json={"ngo_org_id": self.ngo_org},
                              headers={**self.ngo, "Idempotency-Key": uuid.uuid4().hex}, name="/donations/{id}/claim",
                              catch_response=True) as resp:
            if resp.status_code in (201, 409):
                resp.success()
