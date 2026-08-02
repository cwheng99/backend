"""Iteration 3 - Regression + bug-fix tests.

Covers:
- GET /api/packages/lookup?tracking_number=X
- POST /api/packages/batch-status (new BatchStatusResponse: updated/skipped/created)
- Idempotency: skipping already picked_up packages does not touch the record
- Mixed input (updated + skipped + created)
- Existing endpoints regression (POST/GET/PATCH /packages, /stats, /batch-add)
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL is required"
API = BASE_URL.rstrip("/") + "/api"

_created_ids: list[str] = []


@pytest.fixture(scope="module")
def s():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session
    # Best-effort cleanup
    for pid in _created_ids:
        try:
            session.delete(f"{API}/packages/{pid}")
        except Exception:
            pass
    session.close()


def _create(s, **kwargs):
    r = s.post(f"{API}/packages", json=kwargs)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    _created_ids.append(pid)
    return r.json()


# --- Health / basic ---
def test_root(s):
    r = s.get(f"{API}/")
    assert r.status_code == 200


def test_stats_shape(s):
    r = s.get(f"{API}/packages/stats")
    assert r.status_code == 200
    d = r.json()
    for k in ("today", "week", "month", "today_pending", "today_picked_up", "today_returned"):
        assert k in d


# --- Lookup endpoint ---
def test_lookup_returns_null_when_not_found(s):
    r = s.get(f"{API}/packages/lookup", params={"tracking_number": "TEST_NONEXIST_ZZZZZ"})
    assert r.status_code == 200
    assert r.json() is None


def test_lookup_returns_most_recent(s):
    tn = "TEST_TN_LOOKUP_A"
    p1 = _create(s, tracking_number=tn, recipient_name="TEST_old")
    time.sleep(0.05)
    p2 = _create(s, tracking_number=tn, recipient_name="TEST_new")
    r = s.get(f"{API}/packages/lookup", params={"tracking_number": tn})
    assert r.status_code == 200
    body = r.json()
    assert body is not None
    assert body["id"] == p2["id"]
    assert body["status"] == "pending"
    assert "tracking_number" in body


# --- batch-status new response shape ---
def test_batch_status_shape_and_skip(s):
    # Seed: package already picked_up
    tn = "TEST_TN_BATCH_SKIP"
    pkg = _create(s, tracking_number=tn)
    # First move it to picked_up
    r0 = s.post(f"{API}/packages/batch-status", json={
        "items": [{"id": pkg["id"]}], "status": "picked_up"
    })
    assert r0.status_code == 200
    d0 = r0.json()
    assert isinstance(d0, dict)
    assert set(d0.keys()) == {"updated", "skipped", "created"}
    assert len(d0["updated"]) == 1
    assert len(d0["skipped"]) == 0
    assert len(d0["created"]) == 0
    picked_ts = d0["updated"][0]["timestamp"]

    # Capture DB state before second call
    look1 = s.get(f"{API}/packages/lookup", params={"tracking_number": tn}).json()
    assert look1["status"] == "picked_up"

    # Now call again — should be SKIPPED, NO changes
    r1 = s.post(f"{API}/packages/batch-status", json={
        "items": [{"tracking_number": tn}], "status": "picked_up"
    })
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["updated"]) == 0
    assert len(d1["skipped"]) == 1
    assert len(d1["created"]) == 0
    assert d1["skipped"][0]["tracking_number"] == tn
    assert d1["skipped"][0]["status"] == "picked_up"

    # Verify DB state unchanged (same id, same timestamp)
    look2 = s.get(f"{API}/packages/lookup", params={"tracking_number": tn}).json()
    assert look2["id"] == pkg["id"]
    assert look2["status"] == "picked_up"
    assert look2["timestamp"] == picked_ts


def test_batch_status_mixed_updated_skipped_created(s):
    tn_skip = "TEST_TN_MIX_SKIP"
    tn_update = "TEST_TN_MIX_UPD"
    tn_new = "TEST_TN_MIX_NEW"

    # tn_skip: already picked_up
    p_skip = _create(s, tracking_number=tn_skip)
    r = s.post(f"{API}/packages/batch-status", json={"items": [{"id": p_skip["id"]}], "status": "picked_up"})
    assert r.status_code == 200

    # tn_update: currently pending
    p_upd = _create(s, tracking_number=tn_update)

    # tn_new: not created yet — batch-status should create
    payload = {
        "items": [
            {"tracking_number": tn_skip},
            {"tracking_number": tn_update},
            {"tracking_number": tn_new},
        ],
        "status": "picked_up",
    }
    r = s.post(f"{API}/packages/batch-status", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["updated"]) == 1
    assert d["updated"][0]["tracking_number"] == tn_update
    assert len(d["skipped"]) == 1
    assert d["skipped"][0]["tracking_number"] == tn_skip
    assert len(d["created"]) == 1
    assert d["created"][0]["tracking_number"] == tn_new
    assert d["created"][0]["status"] == "picked_up"
    _created_ids.append(d["created"][0]["id"])


def test_no_records_deleted(s):
    # Seed 3 pending packages
    ids = [_create(s, tracking_number=f"TEST_TN_NODEL_{i}")["id"] for i in range(3)]
    payload = {"items": [{"id": pid} for pid in ids], "status": "picked_up"}
    r = s.post(f"{API}/packages/batch-status", json=payload)
    assert r.status_code == 200
    # Verify all 3 still exist
    for pid in ids:
        got = s.get(f"{API}/packages", params={"search": pid})  # can't fetch by id, use list
        # Instead, use lookup by tracking
        pass
    for pid in ids:
        # Alternative: use PATCH endpoint to check existence via 404 vs 200
        r2 = s.patch(f"{API}/packages/{pid}/status", json={"status": "picked_up"})
        # already picked_up, but PATCH still returns 200
        assert r2.status_code == 200


def test_batch_status_invalid_status(s):
    r = s.post(f"{API}/packages/batch-status", json={
        "items": [{"tracking_number": "TEST_x"}], "status": "not_a_status"
    })
    assert r.status_code == 400


def test_batch_status_empty(s):
    r = s.post(f"{API}/packages/batch-status", json={"items": [], "status": "picked_up"})
    assert r.status_code == 200
    d = r.json()
    assert d == {"updated": [], "skipped": [], "created": []}


# --- Existing endpoints regression ---
def test_batch_add_still_works(s):
    r = s.post(f"{API}/packages/batch-add", json={
        "items": [
            {"tracking_number": "TEST_TN_BADD_1", "recipient_name": "TEST_r1"},
            {"tracking_number": "TEST_TN_BADD_2"},
        ],
        "status": "pending",
    })
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) == 2
    for p in arr:
        assert p["status"] == "pending"
        _created_ids.append(p["id"])


def test_patch_status_by_id(s):
    p = _create(s, tracking_number="TEST_TN_PATCH_1")
    r = s.patch(f"{API}/packages/{p['id']}/status", json={"status": "returned"})
    assert r.status_code == 200
    assert r.json()["status"] == "returned"


def test_get_packages_search(s):
    tn = "TEST_TN_SEARCH_UNIQ_QQQ"
    _create(s, tracking_number=tn, recipient_name="TEST_search")
    r = s.get(f"{API}/packages", params={"search": tn, "limit": 5})
    assert r.status_code == 200
    hits = r.json()
    assert any(p["tracking_number"] == tn for p in hits)


def test_export_csv(s):
    r = s.get(f"{API}/packages/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    body = r.text
    # header row
    assert "Date,Time,Status,Recipient,Phone,Tracking Number,Notes" in body
