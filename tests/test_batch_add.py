"""Regression + new tests for POST /api/packages/batch-add and existing package endpoints."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
BASE_URL = (BASE_URL or "").rstrip("/")


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def cleanup_ids():
    ids = []
    yield ids
    for pid in ids:
        try:
            requests.delete(f"{BASE_URL}/api/packages/{pid}", timeout=10)
        except Exception:
            pass


# ------- batch-add endpoint tests -------

class TestBatchAdd:
    def test_batch_add_three_items(self, api, cleanup_ids):
        payload = {
            "items": [
                {"tracking_number": "TEST_BA_1"},
                {"tracking_number": "TEST_BA_2", "recipient_name": "Alice"},
                {"tracking_number": "TEST_BA_3", "phone_number": "0100000003", "notes": "n3"},
            ],
            "status": "pending",
        }
        r = api.post(f"{BASE_URL}/api/packages/batch-add", json=payload, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list) and len(data) == 3
        tns = {p["tracking_number"] for p in data}
        assert tns == {"TEST_BA_1", "TEST_BA_2", "TEST_BA_3"}
        for p in data:
            assert p["status"] == "pending"
            assert "id" in p and p["id"]
            assert "_id" not in p  # ObjectId must not leak
            assert p.get("date")  # YYYY-MM-DD MYT
            cleanup_ids.append(p["id"])

        # Verify persistence via GET /api/packages (search by tracking)
        r2 = api.get(f"{BASE_URL}/api/packages", params={"search": "TEST_BA_"}, timeout=15)
        assert r2.status_code == 200
        found = {p["tracking_number"] for p in r2.json()}
        assert {"TEST_BA_1", "TEST_BA_2", "TEST_BA_3"}.issubset(found)

    def test_batch_add_empty(self, api):
        r = api.post(f"{BASE_URL}/api/packages/batch-add", json={"items": []}, timeout=10)
        assert r.status_code == 200
        assert r.json() == []

    def test_batch_add_one_item(self, api, cleanup_ids):
        r = api.post(
            f"{BASE_URL}/api/packages/batch-add",
            json={"items": [{"tracking_number": "TEST_BA_SINGLE"}]},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["tracking_number"] == "TEST_BA_SINGLE"
        assert data[0]["status"] == "pending"
        cleanup_ids.append(data[0]["id"])

    def test_batch_add_default_pending_when_status_missing(self, api, cleanup_ids):
        r = api.post(
            f"{BASE_URL}/api/packages/batch-add",
            json={"items": [{"tracking_number": "TEST_BA_DEF"}]},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()[0]["status"] == "pending"
        cleanup_ids.append(r.json()[0]["id"])

    def test_batch_add_invalid_status_falls_back_to_pending(self, api, cleanup_ids):
        r = api.post(
            f"{BASE_URL}/api/packages/batch-add",
            json={"items": [{"tracking_number": "TEST_BA_BAD"}], "status": "not_a_status"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()[0]["status"] == "pending"
        cleanup_ids.append(r.json()[0]["id"])

    def test_batch_add_skips_completely_empty_items(self, api, cleanup_ids):
        r = api.post(
            f"{BASE_URL}/api/packages/batch-add",
            json={"items": [{}, {"tracking_number": "TEST_BA_MIX"}, {"recipient_name": None}]},
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["tracking_number"] == "TEST_BA_MIX"
        cleanup_ids.append(data[0]["id"])

    def test_batch_add_preserves_existing_records(self, api, cleanup_ids):
        # Create a seed package
        seed = api.post(
            f"{BASE_URL}/api/packages",
            json={"tracking_number": "TEST_BA_SEED"},
            timeout=10,
        )
        assert seed.status_code == 200
        seed_id = seed.json()["id"]
        cleanup_ids.append(seed_id)

        # count before via stats
        before = api.get(f"{BASE_URL}/api/packages/stats", timeout=10).json()

        # Run batch-add
        r = api.post(
            f"{BASE_URL}/api/packages/batch-add",
            json={"items": [{"tracking_number": "TEST_BA_PRES_1"}, {"tracking_number": "TEST_BA_PRES_2"}]},
            timeout=10,
        )
        assert r.status_code == 200
        for p in r.json():
            cleanup_ids.append(p["id"])

        # Verify seed still exists
        got = api.get(f"{BASE_URL}/api/packages", params={"search": "TEST_BA_SEED"}, timeout=10)
        assert got.status_code == 200
        assert any(p["id"] == seed_id for p in got.json())

        # today count went up by 2 (assuming same MYT day)
        after = api.get(f"{BASE_URL}/api/packages/stats", timeout=10).json()
        assert after["today"] >= before["today"] + 2


# ------- regression tests for existing endpoints -------

class TestRegression:
    def test_create_single_package(self, api, cleanup_ids):
        r = api.post(
            f"{BASE_URL}/api/packages",
            json={"tracking_number": "TEST_REG_SINGLE", "recipient_name": "Bob"},
            timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["tracking_number"] == "TEST_REG_SINGLE"
        assert d["status"] == "pending"
        assert "_id" not in d
        cleanup_ids.append(d["id"])

    def test_get_packages_and_stats(self, api):
        r = api.get(f"{BASE_URL}/api/packages", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

        s = api.get(f"{BASE_URL}/api/packages/stats", timeout=10)
        assert s.status_code == 200
        body = s.json()
        for k in ("today", "week", "month", "today_pending", "today_picked_up",
                  "today_returned", "daily_breakdown"):
            assert k in body

    def test_batch_status_update_still_works(self, api, cleanup_ids):
        # create via batch-add
        r = api.post(
            f"{BASE_URL}/api/packages/batch-add",
            json={"items": [{"tracking_number": "TEST_REG_BSTAT"}]},
            timeout=10,
        )
        pid = r.json()[0]["id"]
        cleanup_ids.append(pid)

        u = api.post(
            f"{BASE_URL}/api/packages/batch-status",
            json={"items": [{"id": pid}], "status": "picked_up"},
            timeout=10,
        )
        assert u.status_code == 200
        assert u.json()[0]["status"] == "picked_up"

    def test_patch_status_by_id(self, api, cleanup_ids):
        r = api.post(
            f"{BASE_URL}/api/packages",
            json={"tracking_number": "TEST_REG_PATCH"},
            timeout=10,
        )
        pid = r.json()["id"]
        cleanup_ids.append(pid)

        u = api.patch(
            f"{BASE_URL}/api/packages/{pid}/status",
            json={"status": "returned"},
            timeout=10,
        )
        assert u.status_code == 200
        assert u.json()["status"] == "returned"

    def test_patch_invalid_status(self, api, cleanup_ids):
        r = api.post(f"{BASE_URL}/api/packages", json={"tracking_number": "TEST_REG_BAD"}, timeout=10)
        pid = r.json()["id"]
        cleanup_ids.append(pid)
        u = api.patch(
            f"{BASE_URL}/api/packages/{pid}/status",
            json={"status": "not_valid"},
            timeout=10,
        )
        assert u.status_code == 400
