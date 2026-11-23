"""End-to-end demo against a running API (after `python -m scripts.seed`):
    python -m scripts.demo_flow http://localhost:8000
publish -> match -> claim (race: one wins, one conflicts) -> assign -> pickup -> deliver -> confirm -> impact."""
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000") + "/api/v1"
PW = "demo-password-123"
c = httpx.Client(timeout=15)


def login(email):
    r = c.post(f"{BASE}/auth/login", json={"email": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def step(msg, r):
    print(f"{'OK ' if r.status_code < 400 else 'ERR'} {r.status_code}  {msg}")
    return r


donor, n1, n2 = login("donor@example.com"), login("ngo1@example.com"), login("ngo2@example.com")
v1, v2 = login("volunteer1@example.com"), login("volunteer2@example.com")
org1 = c.get(f"{BASE}/organisations", headers=n1).json()[0]["id"]
org2 = c.get(f"{BASE}/organisations", headers=n2).json()[0]["id"]
for v in (v1, v2):
    c.post(f"{BASE}/volunteers/me/location", json={"latitude": 25.3176, "longitude": 82.9739}, headers=v)

now = datetime.now(timezone.utc)
body = {"title": "Packed vegetarian meals", "category": "PREPARED_MEALS", "quantity": 80, "unit": "PORTIONS",
        "prepared_at": (now - timedelta(minutes=30)).isoformat(), "expires_at": (now + timedelta(hours=5)).isoformat(),
        "pickup_start": (now - timedelta(minutes=5)).isoformat(), "pickup_end": (now + timedelta(hours=2)).isoformat(),
        "location": {"latitude": 25.3176, "longitude": 82.9739}, "allergen_notes": "Contains wheat"}
did = step("create draft", c.post(f"{BASE}/donations", json=body, headers=donor)).json()["id"]
step("publish", c.post(f"{BASE}/donations/{did}/publish", headers=donor))
print("ranked candidates:", [(x["name"], x["score"]) for x in c.get(f"{BASE}/donations/{did}/candidates", headers=donor).json()["items"]])

r1 = step("NGO 1 claims", c.post(f"{BASE}/donations/{did}/claim", json={"ngo_org_id": org1}, headers={**n1, "Idempotency-Key": uuid.uuid4().hex}))
r2 = step("NGO 2 claims (expect 409)", c.post(f"{BASE}/donations/{did}/claim", json={"ngo_org_id": org2}, headers={**n2, "Idempotency-Key": uuid.uuid4().hex}))
print("   ->", r2.json()["error"]["code"])
alloc, task = r1.json()["id"], r1.json()["task_id"]

time.sleep(1)  # let the outbox worker auto-offer the task (or assign explicitly)
t = c.get(f"{BASE}/tasks/{task}", headers=v1)
vol = v1 if t.status_code == 200 and t.json()["status"] == "OFFERED" else v2
if t.status_code != 200 or t.json()["status"] != "OFFERED":
    vid = c.get(f"{BASE}/auth/me", headers=v1).json()["id"]
    step("assign volunteer", c.post(f"{BASE}/allocations/{alloc}/assign", json={"volunteer_id": vid}, headers=n1)); vol = v1
step("volunteer accepts", c.post(f"{BASE}/tasks/{task}/accept", headers=vol))
step("pickup", c.post(f"{BASE}/tasks/{task}/pickup", headers=vol))
step("deliver", c.post(f"{BASE}/tasks/{task}/deliver", headers=vol))
step("NGO confirms receipt", c.post(f"{BASE}/allocations/{alloc}/confirm-receipt", json={"received_quantity": 80, "condition_ok": True}, headers=n1))
print("status history:")
for h in c.get(f"{BASE}/donations/{did}/history", headers=donor).json()["items"]:
    print(f"   {h['entity_type']:<10} {h['from']} -> {h['to']}")
did_org = c.get(f"{BASE}/organisations", headers=donor).json()[0]["id"]
print("donor impact:", c.get(f"{BASE}/organisations/{did_org}/impact", headers=donor).json())
