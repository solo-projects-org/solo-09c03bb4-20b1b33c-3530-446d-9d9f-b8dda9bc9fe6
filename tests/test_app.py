"""End-to-end tests against a temporary SQLite file (WAL, like production)."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

BASE = 1793491200  # 2026-11-01T00:00:00Z, hour-aligned
H = 3600


def hour(n: int) -> int:
    return BASE + n * H


def csv_of(rows: list[tuple[str, int, float]]) -> str:
    lines = ["meter_id,utc_second,kwh"]
    lines += [f"{m},{t},{k}" for m, t, k in rows]
    return "\n".join(lines) + "\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def post_file(client: TestClient, text: str):
    return client.post("/batches", files={"file": ("batch.csv", text, "text/csv")})


def hourly(client: TestClient, meter: str) -> dict[int, dict]:
    resp = client.get("/hourly-usage", params={"meter_id": meter})
    assert resp.status_code == 200
    return {r["hour_start"]: r for r in resp.json()}


def readings(client: TestClient, meter: str) -> list[dict]:
    resp = client.get("/readings", params={"meter_id": meter})
    assert resp.status_code == 200
    return resp.json()


# ---------- baseline & hourly computation ----------

def test_first_reading_is_baseline_only(client):
    resp = post_file(client, csv_of([("m1", hour(0), 100.0)]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rows_inserted"] == 1
    assert hourly(client, "m1") == {}
    assert len(readings(client, "m1")) == 1


def test_adjacent_hours_produce_billable_usage(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.5),
                              ("m1", hour(2), 104.0)]))
    hours = hourly(client, "m1")
    assert hours[hour(0)]["kwh_used"] == 1.5 and hours[hour(0)]["billable"]
    assert hours[hour(1)]["kwh_used"] == 2.5 and hours[hour(1)]["billable"]
    assert hours[hour(0)]["reason"] == "ok"
    assert hour(2) not in hours  # latest reading is the baseline for the next hour


def test_gap_hours_are_unbillable(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(3), 109.0)]))
    hours = hourly(client, "m1")
    for n in (0, 1, 2):
        assert hours[hour(n)]["billable"] is False
        assert hours[hour(n)]["reason"] == "gap"
        assert hours[hour(n)]["kwh_used"] is None


def test_late_readings_fill_gap_and_update_neighbouring_hours(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m1", hour(4), 107.0)]))
    assert hourly(client, "m1")[hour(1)]["reason"] == "gap"

    resp = post_file(client, csv_of([("m1", hour(2), 103.0), ("m1", hour(3), 105.0)]))
    assert resp.status_code == 201
    hours = hourly(client, "m1")
    assert hours[hour(1)]["kwh_used"] == 2.0 and hours[hour(1)]["billable"]
    assert hours[hour(2)]["kwh_used"] == 2.0 and hours[hour(2)]["billable"]
    assert hours[hour(3)]["kwh_used"] == 2.0 and hours[hour(3)]["billable"]
    assert hours[hour(0)]["kwh_used"] == 1.0  # untouched hour still correct


def test_rollback_hour_is_unbillable(client):
    post_file(client, csv_of([("m1", hour(0), 50.0), ("m1", hour(1), 48.0),
                              ("m1", hour(2), 49.5)]))
    hours = hourly(client, "m1")
    assert hours[hour(0)]["billable"] is False
    assert hours[hour(0)]["reason"] == "rollback"
    assert hours[hour(0)]["kwh_used"] is None
    assert hours[hour(1)]["kwh_used"] == 1.5 and hours[hour(1)]["billable"]


# ---------- idempotency & conflicts ----------

def test_identical_retransmission_recorded_once(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    resp = post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    assert resp.status_code == 201
    body = resp.json()
    assert body["rows_inserted"] == 0
    assert body["rows_deduplicated"] == 2
    assert len(readings(client, "m1")) == 2  # still exactly one copy each
    assert hourly(client, "m1")[hour(0)]["kwh_used"] == 1.0


def test_conflicting_value_rejects_whole_batch_and_keeps_prior_state(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    before_readings = readings(client, "m1")
    before_hours = hourly(client, "m1")
    before_batches = client.get("/batches").json()

    resp = post_file(client, csv_of([("m1", hour(1), 999.0), ("m1", hour(2), 105.0)]))
    assert resp.status_code == 409
    assert "999" in resp.json()["detail"]

    assert readings(client, "m1") == before_readings      # no partial writes
    assert hourly(client, "m1") == before_hours
    assert client.get("/batches").json() == before_batches  # failed batch leaves no trace


def test_conflicting_duplicates_inside_one_batch_rejected(client):
    resp = post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(0), 100.5)]))
    assert resp.status_code == 422
    assert readings(client, "m1") == []


# ---------- validation ----------

@pytest.mark.parametrize("row,fragment", [
    ("m1,1793491201,1.0", "aligned"),          # not a whole hour
    ("m1,1793491200,-1.0", ">= 0"),            # negative kwh
    ("m1,1793491200,abc", "not a number"),
    ("m1,1793491200,nan", "finite"),
    (",1793491200,1.0", "meter_id is empty"),
    ("m1,not-a-number,1.0", "non-negative integer"),
])
def test_invalid_rows_rejected(client, row, fragment):
    resp = post_file(client, f"meter_id,utc_second,kwh\n{row}\n")
    assert resp.status_code == 422
    assert fragment in resp.json()["detail"]
    assert readings(client, "m1") == []


def test_missing_and_unknown_columns_rejected(client):
    assert post_file(client, "meter_id,kwh\nm1,1.0\n").status_code == 422
    assert post_file(client, "meter_id,utc_second,kwh,extra\nm1,1793491200,1.0,x\n").status_code == 422


def test_raw_text_csv_body_accepted(client):
    resp = client.post("/batches", content=csv_of([("m1", hour(0), 1.0)]),
                       headers={"content-type": "text/csv"})
    assert resp.status_code == 201, resp.text
    assert len(readings(client, "m1")) == 1


def test_query_window_filters_and_validation(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(5)]))
    resp = client.get("/hourly-usage",
                      params={"meter_id": "m1", "from_second": hour(1), "to_second": hour(2)})
    assert [r["hour_start"] for r in resp.json()] == [hour(1), hour(2)]
    assert client.get("/readings", params={"meter_id": "m1",
                                           "from_second": 1793491201}).status_code == 422
    assert client.get("/readings", params={"meter_id": "m1", "from_second": hour(3),
                                           "to_second": hour(1)}).status_code == 422


# ---------- concurrency: two writers, one shared database file ----------

def test_concurrent_ingests_same_meter_all_survive(client):
    # Two "instances" (threads, separate connections) backfill the same meter
    # with disjoint hours at the same time; every successful batch must persist
    # and hourly usage must match the final readings.
    batches = [
        [("m1", hour(0), 100.0), ("m1", hour(1), 101.0)],
        [("m1", hour(2), 103.0), ("m1", hour(3), 106.0)],
        [("m1", hour(4), 110.0), ("m1", hour(5), 115.0)],
        [("m1", hour(6), 121.0), ("m1", hour(7), 128.0)],
    ]
    results: list = [None] * len(batches)

    def worker(i, rows):
        results[i] = post_file(client, csv_of(rows))

    threads = [threading.Thread(target=worker, args=(i, b)) for i, b in enumerate(batches)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in results)
    assert sum(r.json()["rows_inserted"] for r in results) == 8
    assert len(readings(client, "m1")) == 8

    hours = hourly(client, "m1")
    expected = {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 5.0, 5: 6.0, 6: 7.0}
    for n, want in expected.items():
        assert hours[hour(n)]["kwh_used"] == want, f"hour {n}"
        assert hours[hour(n)]["billable"]


# ---------- sealing / retention ----------

def seal(client: TestClient, meter: str, cutoff: int):
    return client.post("/seals", json={"meter_id": meter, "cutoff_second": cutoff})


def test_seal_requires_reading_at_cutoff(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    resp = seal(client, "m1", hour(2))  # no reading at hour 2
    assert resp.status_code == 409
    assert "no reading" in resp.json()["detail"]
    # nothing changed: raw readings still all there
    assert len(readings(client, "m1")) == 2
    assert client.get("/seals").json() == []


def test_seal_unknown_meter_conflicts(client):
    resp = seal(client, "ghost", hour(0))
    assert resp.status_code == 409
    assert "no reading" in resp.json()["detail"]


def test_seal_purges_raw_readings_but_keeps_hourly_results(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(5)]))
    hours_before = hourly(client, "m1")

    resp = seal(client, "m1", hour(3))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["readings_deleted"] == 3          # hours 0,1,2 purged
    assert body["readings_remaining"] == 2        # cutoff hour 3 + hour 4
    assert body["previous_cutoff_second"] is None
    assert body["idempotent_retry"] is False

    kept = readings(client, "m1")
    assert [r["utc_second"] for r in kept] == [hour(3), hour(4)]

    # frozen hourly results are still queryable and byte-for-byte identical
    assert hourly(client, "m1") == hours_before
    assert client.get("/hourly-usage",
                      params={"meter_id": "m1", "from_second": hour(0),
                              "to_second": hour(2)}).json() == [
        hours_before[hour(0)], hours_before[hour(1)], hours_before[hour(2)]]

    seals = client.get("/seals", params={"meter_id": "m1"}).json()
    assert len(seals) == 1 and seals[0]["cutoff_second"] == hour(3)


def test_seal_idempotent_retry_does_not_purge_again(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(4)]))
    first = seal(client, "m1", hour(2)).json()
    assert first["readings_deleted"] == 2

    retry = seal(client, "m1", hour(2))
    assert retry.status_code == 200
    body = retry.json()
    assert body["idempotent_retry"] is True
    assert body["readings_deleted"] == 0
    assert body["previous_cutoff_second"] == hour(2)
    assert [r["utc_second"] for r in readings(client, "m1")] == [hour(2), hour(3)]


def test_seal_cutoff_can_only_move_forward(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(6)]))
    assert seal(client, "m1", hour(2)).status_code == 200

    resp = seal(client, "m1", hour(1))
    assert resp.status_code == 409
    assert "move forward" in resp.json()["detail"]
    # seal position and purged readings untouched by the failed attempt
    assert client.get("/seals", params={"meter_id": "m1"}).json()[0]["cutoff_second"] == hour(2)
    assert [r["utc_second"] for r in readings(client, "m1")] == \
        [hour(2), hour(3), hour(4), hour(5)]

    # advancing is allowed and only purges the newly covered range
    advanced = seal(client, "m1", hour(4))
    assert advanced.status_code == 200
    body = advanced.json()
    assert body["readings_deleted"] == 2          # hours 2,3 now purged
    assert body["previous_cutoff_second"] == hour(2)
    assert [r["utc_second"] for r in readings(client, "m1")] == [hour(4), hour(5)]
    assert client.get("/seals", params={"meter_id": "m1"}).json()[0]["cutoff_second"] == hour(4)


def test_batch_with_reading_before_cutoff_is_whole_rejected(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(4)]))
    assert seal(client, "m1", hour(2)).status_code == 200

    before_readings = readings(client, "m1")
    before_hours = hourly(client, "m1")
    before_batches = client.get("/batches").json()

    # one offending pre-cutoff row + one valid post-cutoff row: whole batch out
    resp = post_file(client, csv_of([("m1", hour(1), 101.0),
                                     ("m1", hour(4), 105.0)]))
    assert resp.status_code == 409
    assert "sealed" in resp.json()["detail"]

    assert readings(client, "m1") == before_readings
    assert hourly(client, "m1") == before_hours
    assert client.get("/batches").json() == before_batches


def test_reading_at_or_after_cutoff_still_ingested_and_hours_computed(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m1", hour(2), 103.0)]))
    assert seal(client, "m1", hour(2)).status_code == 200

    # late backfill after the cutoff: hours 2..4 get computed normally
    resp = post_file(client, csv_of([("m1", hour(3), 106.0), ("m1", hour(4), 110.0)]))
    assert resp.status_code == 201, resp.text
    hours = hourly(client, "m1")
    assert hours[hour(2)]["kwh_used"] == 3.0 and hours[hour(2)]["billable"]
    assert hours[hour(3)]["kwh_used"] == 4.0 and hours[hour(3)]["billable"]
    assert hours[hour(0)]["kwh_used"] == 1.0   # frozen hour still there

    # identical retransmission of the cutoff anchor itself stays allowed
    again = post_file(client, csv_of([("m1", hour(2), 103.0)]))
    assert again.status_code == 201
    assert again.json()["rows_deduplicated"] == 1


def test_frozen_hours_never_recomputed_even_with_late_anchor_neighbour(client):
    # A late reading right at/after the cutoff would normally recompute the
    # hour starting at the cutoff; earlier hours must stay exactly as frozen.
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m1", hour(2), 103.0), ("m1", hour(4), 109.0)]))
    frozen = hourly(client, "m1")
    assert seal(client, "m1", hour(2)).status_code == 200

    # fill hour 3 late: hour 2 is recomputed (>= cutoff), hour 1 is frozen
    post_file(client, csv_of([("m1", hour(3), 106.0)]))
    hours = hourly(client, "m1")
    assert hours[hour(1)] == frozen[hour(1)]
    assert hours[hour(0)] == frozen[hour(0)]
    assert hours[hour(2)]["kwh_used"] == 3.0
    assert hours[hour(3)]["kwh_used"] == 3.0


def test_late_backfill_after_seal_works_with_pre_cutoff_raw_purged(client):
    # Pre-cutoff readings are physically gone; a later backfill must still
    # recompute only post-cutoff hours and never resurrect or touch frozen ones.
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m1", hour(2), 103.0), ("m1", hour(4), 109.0)]))
    frozen = hourly(client, "m1")
    assert seal(client, "m1", hour(2)).status_code == 200
    assert [r["utc_second"] for r in readings(client, "m1")] == [hour(2), hour(4)]
    assert frozen[hour(2)]["reason"] == "gap"   # 2->4 gap before the late fill

    post_file(client, csv_of([("m1", hour(3), 106.0)]))
    hours = hourly(client, "m1")
    assert hours[hour(2)]["billable"] and hours[hour(2)]["kwh_used"] == 3.0
    assert hours[hour(3)]["billable"] and hours[hour(3)]["kwh_used"] == 3.0
    assert hours[hour(0)] == frozen[hour(0)] and hours[hour(1)] == frozen[hour(1)]


def test_frozen_gap_and_rollback_reasons_remain_queryable(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(3), 109.0),
                              ("m2", hour(0), 50.0), ("m2", hour(1), 48.0)]))
    assert seal(client, "m1", hour(3)).status_code == 200
    assert seal(client, "m2", hour(1)).status_code == 200

    m1 = hourly(client, "m1")
    for n in (0, 1, 2):
        assert m1[hour(n)]["reason"] == "gap" and m1[hour(n)]["billable"] is False
    m2 = hourly(client, "m2")
    assert m2[hour(0)]["reason"] == "rollback" and m2[hour(0)]["billable"] is False
    # raw evidence is gone but billing results survive
    assert [r["utc_second"] for r in readings(client, "m1")] == [hour(3)]
    assert [r["utc_second"] for r in readings(client, "m2")] == [hour(1)]

def test_sealing_one_meter_does_not_touch_other_meters(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m2", hour(0), 200.0), ("m2", hour(1), 202.0)]))
    assert seal(client, "m1", hour(1)).status_code == 200
    assert [r["meter_id"] for r in readings(client, "m1")] == ["m1"]
    assert len(readings(client, "m2")) == 2
    assert len(client.get("/seals").json()) == 1


def test_multi_meter_batch_rejected_if_any_meter_violates_seal(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m2", hour(0), 200.0), ("m2", hour(1), 202.0)]))
    assert seal(client, "m1", hour(1)).status_code == 200

    before = {m: readings(client, m) for m in ("m1", "m2")}
    # m2 rows are fine, but the m1 row at hour 0 is before m1's cutoff
    resp = post_file(client, csv_of([("m2", hour(2), 205.0),
                                     ("m1", hour(0), 100.0)]))
    assert resp.status_code == 409
    assert "m1" in resp.json()["detail"]
    assert readings(client, "m1") == before["m1"]
    assert readings(client, "m2") == before["m2"]  # the innocent meter also untouched


def test_seal_validation_errors(client):
    post_file(client, csv_of([("m1", hour(0), 100.0)]))
    assert seal(client, "m1", hour(0) + 7).status_code == 422   # not hour-aligned
    assert client.post("/seals", json={"meter_id": "m1", "cutoff_second": -1}).status_code == 422
    assert client.post("/seals", json={"meter_id": "",
                                       "cutoff_second": hour(0)}).status_code == 422
    assert client.post("/seals", json={"cutoff_second": hour(0)}).status_code == 422
    assert len(readings(client, "m1")) == 1  # failed validation changed nothing


def test_concurrent_seal_and_ingest_share_one_consistent_state(client):
    # Two "instances" (threads, separate connections) race a seal against
    # backfills. Every commit is atomic, so the final state must satisfy every
    # seal invariant no matter which writer wins first.
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(6)]))
    outcomes = {}

    def do_seal():
        outcomes["seal"] = seal(client, "m1", hour(3))

    # a pre-cutoff late reading: accepted only if it wins before the seal
    def do_late():
        outcomes["late"] = post_file(client, csv_of([("m1", hour(2), 102.0)]))

    # a post-cutoff reading: must always be accepted
    def do_future():
        outcomes["future"] = post_file(client, csv_of([("m1", hour(6), 107.0)]))

    threads = [threading.Thread(target=do_seal),
               threading.Thread(target=do_late),
               threading.Thread(target=do_future)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["seal"].status_code == 200
    assert outcomes["future"].status_code == 201
    assert outcomes["late"].status_code in (201, 409)

    kept = {r["utc_second"]: r["kwh"] for r in readings(client, "m1")}
    assert all(ts >= hour(3) for ts in kept)                 # nothing pre-cutoff survives
    assert hour(3) in kept and hour(6) in kept               # anchor + future reading
    assert client.get("/seals", params={"meter_id": "m1"}).json()[0]["cutoff_second"] == hour(3)

    hours = hourly(client, "m1")
    for n in range(3):                                      # frozen hours intact
        assert hour(n) in hours
    assert hours[hour(5)]["kwh_used"] == 2.0                # post-cutoff computed


def test_concurrent_same_cutoff_seals_both_succeed_without_double_purge(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(4)]))
    results = []

    def worker():
        results.append(seal(client, "m1", hour(2)))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r.status_code for r in results) == [200, 200]
    deletions = sorted(r.json()["readings_deleted"] for r in results)
    assert deletions == [0, 2]                               # exactly one purge happened
    assert len(client.get("/seals").json()) == 1
    assert [r["utc_second"] for r in readings(client, "m1")] == [hour(2), hour(3)]
