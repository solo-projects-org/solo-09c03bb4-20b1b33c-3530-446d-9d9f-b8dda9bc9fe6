#!/usr/bin/env bash
# End-to-end demo against the two docker-compose instances.
# Usage: ./scripts/demo.sh   (expects api-a on :8001 and api-b on :8002)
set -euo pipefail

API_A="${API_A:-http://localhost:8001}"
API_B="${API_B:-http://localhost:8002}"
SAMPLES="$(cd "$(dirname "$0")/.." && pwd)/samples"
METER="meter-east-01"
T0=1793491200   # 2026-11-01T00:00:00Z
T5=1793509200   # 2026-11-01T05:00:00Z

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# post_batch <api> <csv-file>: prints "HTTP <code>" plus the pretty JSON body
post_batch() {
  local out
  out="$(curl -s -w '\n%{http_code}' -F "file=@$2;type=text/csv" "$1/batches")"
  echo "  HTTP $(echo "$out" | tail -1)"
  echo "$out" | head -n -1 | jq . | sed 's/^/  /'
}

query_hours() {
  curl -s "$1/hourly-usage?meter_id=$2&from_second=$T0&to_second=$T5" |
    jq -r '.[] | "  \(.hour_start_utc)  billable=\(.billable)  kwh_used=\(.kwh_used)  reason=\(.reason)"'
}

say "0. wait for both instances"
for api in "$API_A" "$API_B"; do
  for i in $(seq 1 60); do
    curl -sf "$api/health" >/dev/null 2>&1 && break
    [ "$i" = 60 ] && { echo "$api not healthy"; exit 1; }
    sleep 1
  done
  echo "  $api healthy"
done

say "1. api-a <- batch1 (backfill with a 2-hour gap: readings at 00,01,02,05)"
post_batch "$API_A" "$SAMPLES/batch1_initial_with_gap.csv"
echo "hourly usage of $METER (hours 02,03,04 must be unbillable gap):"
query_hours "$API_A" "$METER"

say "2. api-b <- batch2 (LATE readings 03,04 fill the gap; neighbours update together)"
post_batch "$API_B" "$SAMPLES/batch2_late_gap_fill.csv"
echo "hourly usage after the late fill (all hours billable now):"
query_hours "$API_B" "$METER"

say "3. api-a <- batch3 (identical retransmission: recorded once, deduplicated)"
post_batch "$API_A" "$SAMPLES/batch3_retransmit_same_values.csv"
echo "raw readings of $METER (still one row per second):"
curl -s "$API_A/readings?meter_id=$METER&from_second=$T0&to_second=$T5" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'

say "4. api-b <- batch4 (conflicting value for an existing second: whole batch rejected)"
post_batch "$API_B" "$SAMPLES/batch4_conflicting_value.csv"
echo "state after the rejected batch (unchanged):"
query_hours "$API_A" "$METER"

say "5. api-a <- batch5 (meter-west-02 rolls back 860 -> 855)"
post_batch "$API_A" "$SAMPLES/batch5_meter_rollback.csv"
echo "hourly usage of meter-west-02 (hour 00 unbillable rollback, hour 01 billable):"
query_hours "$API_A" "meter-west-02"

say "6. concurrency: api-a and api-b backfill meter-north-03 at the same time"
post_batch "$API_A" "$SAMPLES/batch6a_concurrent.csv" > /tmp/demo_cc_a.txt &
PID_A=$!
post_batch "$API_B" "$SAMPLES/batch6b_concurrent.csv" > /tmp/demo_cc_b.txt &
PID_B=$!
wait "$PID_A" "$PID_B"
echo "api-a:"; cat /tmp/demo_cc_a.txt
echo "api-b:"; cat /tmp/demo_cc_b.txt
echo "all readings of meter-north-03 (both successful batches preserved):"
curl -s "$API_A/readings?meter_id=meter-north-03" | jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
echo "hourly usage of meter-north-03 (consistent with the final readings):"
query_hours "$API_B" "meter-north-03"

# ---------- sealing / retention ----------
CUTOFF=1793502000   # 2026-11-01T03:00:00Z (hour-aligned, a reading exists there after step 2)

seal_meter() {  # seal_meter <api> <cutoff-second>
  local out
  out="$(curl -s -w '\n%{http_code}' -X POST "$1/seals" \
    -H 'content-type: application/json' \
    -d "{\"meter_id\":\"$METER\",\"cutoff_second\":$2}")"
  echo "  HTTP $(echo "$out" | tail -1)"
  echo "$out" | head -n -1 | jq . | sed 's/^/  /'
}

say "7. state before sealing $METER"
echo "raw readings (00:00..05:00):"
curl -s "$API_A/readings?meter_id=$METER&from_second=$T0&to_second=$T5" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
echo "hourly results that must survive the purge:"
query_hours "$API_A" "$METER"

say "8. api-a <- seal at 03:00:00Z (purging raw readings before the cutoff)"
seal_meter "$API_A" "$CUTOFF"
echo "raw readings after sealing (03:00 anchor and later only):"
curl -s "$API_A/readings?meter_id=$METER" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
echo "hourly results still queryable and unchanged:"
query_hours "$API_A" "$METER"
echo "seal registry:"
curl -s "$API_A/seals" | jq -r '.[] | "  meter=\(.meter_id) cutoff=\(.cutoff_utc)"'

say "9. retry the SAME cutoff on api-b (idempotent: nothing purged twice)"
seal_meter "$API_B" "$CUTOFF"

say "10. move the cutoff BACKWARDS to 02:00:00Z (rejected with HTTP 409)"
seal_meter "$API_A" 1793498400

say "11. api-b <- batch7: carries an 02:00 reading earlier than the cutoff (whole batch rejected)"
post_batch "$API_B" "$SAMPLES/batch7_reading_before_seal_cutoff.csv"
echo "state after the rejected batch (raw readings and hourly results unchanged):"
curl -s "$API_A/readings?meter_id=$METER" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
query_hours "$API_A" "$METER"

say "12. api-a <- batch8: backfill at/after the cutoff (accepted, new hours computed)"
post_batch "$API_A" "$SAMPLES/batch8_backfill_after_seal.csv"
echo "hourly usage 00:00..07:00 (frozen hours unchanged, new hours billable):"
curl -s "$API_A/hourly-usage?meter_id=$METER&from_second=$T0&to_second=1793516400" |
  jq -r '.[] | "  \(.hour_start_utc)  billable=\(.billable)  kwh_used=\(.kwh_used)  reason=\(.reason)"'

say "13. advance the cutoff forward to 06:00:00Z (purges the 03:00-05:00 raw readings)"
seal_meter "$API_B" 1793512800
echo "remaining raw readings (06:00 anchor and later only):"
curl -s "$API_A/readings?meter_id=$METER" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
echo "all hourly results remain available for the billing team:"
curl -s "$API_A/hourly-usage?meter_id=$METER&from_second=$T0&to_second=1793516400" |
  jq -r '.[] | "  \(.hour_start_utc)  billable=\(.billable)  kwh_used=\(.kwh_used)  reason=\(.reason)"'

say "demo finished"
