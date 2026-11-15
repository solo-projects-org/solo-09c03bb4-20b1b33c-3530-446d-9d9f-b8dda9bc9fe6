"""FastAPI surface: batch ingest (CSV upload) plus query endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from .db import connect, init_db
from .logic import (HOUR, ConflictError, ValidationError, ingest, list_seals,
                    parse_csv, seal_meter, utc_iso)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Park Energy Backfill API",
    version="1.1.0",
    description=(
        "Receives backfilled cumulative meter readings (CSV), keeps exactly one "
        "copy of identical retransmissions, rejects batches that contradict "
        "stored readings, and derives billable hourly kWh for the billing team. "
        "POST /seals archives a meter at a UTC hour cutoff: raw readings before "
        "the cutoff are purged while the already-derived hourly results stay "
        "queryable and frozen. Upload samples via the file picker below, then "
        "query /readings and /hourly-usage."),
    lifespan=lifespan,
)


# ---------- schemas ----------

class IngestResponse(BaseModel):
    batch_id: str
    received_at: str
    rows_total: int
    rows_inserted: int
    rows_deduplicated: int
    meters: list[str]
    hourly_usage_updated: int


class ReadingOut(BaseModel):
    meter_id: str
    utc_second: int
    utc_time: str
    kwh: float
    batch_id: str


class HourlyUsageOut(BaseModel):
    meter_id: str
    hour_start: int
    hour_start_utc: str
    kwh_used: Optional[float]
    billable: bool
    reason: str  # ok | gap | rollback


class BatchOut(BaseModel):
    batch_id: str
    received_at: str
    rows_total: int
    rows_inserted: int
    rows_deduplicated: int


class SealRequest(BaseModel):
    meter_id: str = Field(..., min_length=1, max_length=128,
                          description="meter identifier to seal")
    cutoff_second: int = Field(
        ..., ge=0,
        description="UTC hour-aligned cutoff second; a reading at this exact "
                    "hour must already exist")


class SealResponse(BaseModel):
    meter_id: str
    cutoff_second: int
    cutoff_utc: str
    previous_cutoff_second: Optional[int]
    idempotent_retry: bool
    readings_deleted: int
    readings_remaining: int
    sealed_at: str


class SealOut(BaseModel):
    meter_id: str
    cutoff_second: int
    cutoff_utc: str
    sealed_at: str


# ---------- ingest ----------

@app.post("/batches", response_model=IngestResponse, status_code=201,
          summary="Upload a CSV batch of cumulative readings",
          responses={
              409: {"description": "Batch contradicts stored readings or contains a "
                                    "reading before the meter's seal cutoff; nothing was saved"},
              415: {"description": "Unsupported content type"},
              422: {"description": "CSV failed validation; nothing was saved"},
          })
async def create_batch(request: Request,
                       file: Optional[UploadFile] = File(
                           default=None,
                           description="CSV file with columns meter_id,utc_second,kwh")):
    """Accept the CSV either as a multipart file upload (Swagger file picker)
    or as a raw `text/csv` request body."""
    text = await _extract_csv_text(request, file)

    try:
        rows = parse_csv(text)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        return ingest(rows)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


async def _extract_csv_text(request: Request, file: Optional[UploadFile]) -> str:
    content_type = request.headers.get("content-type", "")
    if file is not None:
        data = await file.read()
    elif any(content_type.startswith(t) for t in
             ("text/csv", "text/plain", "application/octet-stream")):
        data = await request.body()
    else:
        raise HTTPException(
            status_code=415,
            detail="send the CSV as multipart/form-data file upload or as a text/csv body")
    try:
        return data.decode("utf-8-sig")  # tolerate a BOM
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV must be UTF-8 encoded")


# ---------- sealing / retention ----------

@app.post("/seals", response_model=SealResponse,
          summary="Seal a meter at a UTC hour cutoff and purge earlier raw readings",
          responses={
              409: {"description": "No reading at the cutoff, or cutoff moved "
                                    "backwards; nothing was changed"},
              422: {"description": "cutoff_second missing, negative or not hour-aligned"},
          })
def create_seal(payload: SealRequest):
    """Archive a meter at `cutoff_second` (a UTC hour boundary).

    - A reading at exactly the cutoff hour must already exist.
    - Raw readings strictly before the cutoff are deleted; the hourly kWh,
      billable flag and reason already derived for earlier hours remain
      queryable through /hourly-usage and never change afterwards.
    - Hours starting at or after the cutoff keep accepting late backfills.
    - Batches containing any reading of this meter earlier than the cutoff are
      rejected wholesale by POST /batches.
    - Retrying the same cutoff changes nothing (idempotent); the cutoff may
      only move forward.
    """
    if payload.cutoff_second % HOUR != 0:
        raise HTTPException(
            status_code=422,
            detail=f"cutoff_second must be aligned to a UTC hour, got {payload.cutoff_second}")
    try:
        return seal_meter(payload.meter_id, payload.cutoff_second)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/seals", response_model=list[SealOut],
         summary="Seal positions of meters (optionally one meter)")
def get_seals(meter_id: Optional[str] = Query(None, description="filter by meter")):
    return list_seals(meter_id)


# ---------- queries ----------

def _window(from_second: Optional[int], to_second: Optional[int]) -> tuple[int, int]:
    for name, value in (("from_second", from_second), ("to_second", to_second)):
        if value is not None and (value < 0 or value % HOUR != 0):
            raise HTTPException(
                status_code=422,
                detail=f"{name} must be a non-negative UTC hour-aligned second, got {value}")
    lo = from_second if from_second is not None else 0
    hi = to_second if to_second is not None else 2**63 - 1
    if lo > hi:
        raise HTTPException(status_code=422, detail="from_second must be <= to_second")
    return lo, hi


@app.get("/readings", response_model=list[ReadingOut],
         summary="Raw cumulative readings of one meter in a time window")
def get_readings(meter_id: str = Query(..., description="meter identifier"),
                 from_second: Optional[int] = Query(None, description="UTC hour-aligned, inclusive"),
                 to_second: Optional[int] = Query(None, description="UTC hour-aligned, inclusive")):
    lo, hi = _window(from_second, to_second)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT meter_id, utc_second, kwh, batch_id FROM readings "
            "WHERE meter_id = ? AND utc_second BETWEEN ? AND ? ORDER BY utc_second",
            (meter_id, lo, hi)).fetchall()
    finally:
        conn.close()
    return [ReadingOut(meter_id=r["meter_id"], utc_second=r["utc_second"],
                       utc_time=utc_iso(r["utc_second"]), kwh=r["kwh"],
                       batch_id=r["batch_id"]) for r in rows]


@app.get("/hourly-usage", response_model=list[HourlyUsageOut],
         summary="Hourly kWh usage with billable flag and reason")
def get_hourly_usage(meter_id: str = Query(..., description="meter identifier"),
                     from_second: Optional[int] = Query(None, description="UTC hour-aligned, inclusive"),
                     to_second: Optional[int] = Query(None, description="UTC hour-aligned, inclusive")):
    lo, hi = _window(from_second, to_second)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT meter_id, hour_start, kwh_used, billable, reason FROM hourly_usage "
            "WHERE meter_id = ? AND hour_start BETWEEN ? AND ? ORDER BY hour_start",
            (meter_id, lo, hi)).fetchall()
    finally:
        conn.close()
    return [HourlyUsageOut(meter_id=r["meter_id"], hour_start=r["hour_start"],
                           hour_start_utc=utc_iso(r["hour_start"]),
                           kwh_used=r["kwh_used"], billable=bool(r["billable"]),
                           reason=r["reason"]) for r in rows]


@app.get("/batches", response_model=list[BatchOut],
         summary="Audit trail of accepted batches")
def list_batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT batch_id, received_at, rows_total, rows_inserted, rows_deduplicated "
            "FROM batches ORDER BY received_at DESC, batch_id").fetchall()
    finally:
        conn.close()
    return [BatchOut(**dict(r)) for r in rows]


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}
