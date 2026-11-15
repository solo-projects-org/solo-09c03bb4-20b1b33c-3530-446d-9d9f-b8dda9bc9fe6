# 园区能源补传数据 API（Park Energy Backfill API）

网关断网恢复后，电表累计读数会成批补传。本服务接收补传 CSV，校验并幂等入库，
按小时算出**可计费电量**交给计费组复核；迟到读数填补缺口时，前后小时的结果在同一事务里一起更新。

园区数据员按保留期限清理原始读数时，可调用**封存 API**：按电表和 UTC 整点截止时刻封存，
清除截止时刻之前的原始读数，而截止前已核算的小时电量、可计费状态及原因永久保留、不再变化。

- FastAPI + SQLite（WAL），两个 API 实例共享同一个数据库文件
- `docker compose up` 一键启动两个实例（`:8001` / `:8002`），Swagger 文档开箱即用
- 本地样例覆盖：缺口、迟到填洞、重传去重、异值拒批、表码回退、双实例并发补传、封存清理、迟到读数拒批

## 快速开始

```bash
docker compose up --build
# Swagger UI: http://localhost:8001/docs  或  http://localhost:8002/docs
```

跑完整演示（含迟到填洞、双实例并发与封存全流程）：

```bash
./scripts/demo.sh
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=./data/energy.db uvicorn app.main:app --port 8001 &
DB_PATH=./data/energy.db uvicorn app.main:app --port 8002 &
python -m pytest tests/ -q
```

## 数据规则

**CSV 格式**：表头必须是 `meter_id,utc_second,kwh` 三列（UTF-8，可带 BOM）。

- `utc_second`：UTC 整点秒（能被 3600 整除的非负整数）
- `kwh`：电表累计读数，有限且 ≥ 0
- 任一行不合法 → 整批拒绝（HTTP 422），库中已有结果不受影响

**幂等与冲突**：

- 同表同秒**同值**重传：只记一次（响应计入 `rows_deduplicated`）
- 同表同秒**异值**（含批内自相矛盾）：整批拒绝（HTTP 409），不写任何一行

**小时电量**（`hourly_usage`）：

- 每块表的首个读数只作基线，不产生小时电量
- 相邻两条读数恰好相隔 1 小时且未回退 → 该小时 `billable=true`，`kwh_used` 为差值
- 读数缺口内的每个小时 → `billable=false, reason=gap`
- 表码回退对应的小时 → `billable=false, reason=rollback`
- 迟到读数填补缺口时，受影响窗口（含前后相邻小时）在同一事务内重算

**封存与保留期限**（`seals`，`POST /seals`）：

- 请求体 `{"meter_id": "...", "cutoff_second": <UTC 整点秒>}`；**截止时刻必须已有读数**，否则 HTTP 409、什么都不改
- 清除该表**严格早于截止时刻**的原始读数；截止时刻的读数保留，作为其后小时的基线
- 截止前已核算的小时电量、`billable` 与 `reason` 保持可查且**永不重算、不再变化**（原始读数删除后依然可查）
- 截止时刻起的小时仍按后续补传正常核算（重算窗口被钳制在截止时刻及以后）
- 批次只要含该表**早于截止时刻**的读数就**整批拒绝**（HTTP 409），库中状态不变
- 同一截止时刻重试幂等：不重复清理（`idempotent_retry=true`、`readings_deleted=0`）
- 截止时刻**只能前移**（取更大的整点）；请求更早的截止时刻返回 HTTP 409
- 清理、封存位置与保留的小时结果在同一个 `BEGIN IMMEDIATE` 事务内共同生效，失败整体回滚

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/batches` | 上传 CSV 批次：multipart 文件（Swagger 文件选择器）或 `text/csv` 原始体；含封存截止前读数的批次整批拒绝（409） |
| `GET` | `/readings` | 按 `meter_id` + `from_second`/`to_second`（均为整点秒、闭区间）查原始读数（封存时刻之前的已被清除） |
| `GET` | `/hourly-usage` | 按表和时间窗查小时电量、`billable` 与 `reason`（`ok`/`gap`/`rollback`）；封存前的结果冻结保留 |
| `GET` | `/batches` | 已接受批次的审计列表 |
| `POST` | `/seals` | 按电表和 UTC 整点截止时刻封存并清除更早的原始读数 |
| `GET` | `/seals` | 查封存位置（可带 `meter_id` 过滤） |

`POST /batches` 响应示例：

```json
{
  "batch_id": "96a1464398704927a5051026da511afb",
  "received_at": "2026-11-14T20:58:10Z",
  "rows_total": 2,
  "rows_inserted": 2,
  "rows_deduplicated": 0,
  "meters": ["meter-east-01"],
  "hourly_usage_updated": 3
}
```

`POST /seals` 请求与响应示例：

```bash
curl -X POST http://localhost:8001/seals -H 'content-type: application/json' \
  -d '{"meter_id":"meter-east-01","cutoff_second":1793502000}'
```

```json
{
  "meter_id": "meter-east-01",
  "cutoff_second": 1793502000,
  "cutoff_utc": "2026-11-01T03:00:00Z",
  "previous_cutoff_second": null,
  "idempotent_retry": false,
  "readings_deleted": 3,
  "readings_remaining": 3,
  "sealed_at": "2026-11-15T08:30:00Z"
}
```

curl 示例：

```bash
curl -F "file=@samples/batch1_initial_with_gap.csv;type=text/csv" http://localhost:8001/batches
curl -X POST -H "content-type: text/csv" --data-binary @samples/batch2_late_gap_fill.csv http://localhost:8002/batches
curl "http://localhost:8001/hourly-usage?meter_id=meter-east-01&from_second=1793491200&to_second=1793509200"
curl "http://localhost:8001/seals?meter_id=meter-east-01"
```

## Swagger 导入本地样例

打开 `http://localhost:8001/docs` → `POST /batches` → **Try it out** → 文件选择器依次上传：

1. `samples/batch1_initial_with_gap.csv` — 00/01/02/05 点读数，02–05 出现 3 小时缺口
2. `GET /hourly-usage?meter_id=meter-east-01` — 可见 02/03/04 点 `reason=gap`
3. `samples/batch2_late_gap_fill.csv` — 迟到的 03/04 点读数；再查可见缺口各小时已变为可计费
4. `samples/batch3_retransmit_same_values.csv` — 同值重传，`rows_inserted=0`、`rows_deduplicated=2`
5. `samples/batch4_conflicting_value.csv` — 异值冲突，HTTP 409，已存数据不变
6. `samples/batch5_meter_rollback.csv` — `meter-west-02` 表码回退，对应小时不可计费
7. `samples/batch6a_concurrent.csv` / `batch6b_concurrent.csv` — 分别发往两个实例（演示并发补传同一表）

### Swagger 演示封存（建议在上面 1–3 步之后）

在 `POST /seals` → **Try it out** 中改请求体，截止时刻用整点秒（`meter-east-01` 在 03:00 已有读数）：

8. 请求体 `{"meter_id":"meter-east-01","cutoff_second":1793502000}` 封存于 **03:00:00Z**
   - `GET /readings?meter_id=meter-east-01` — 只剩 03:00 及以后的原始读数，00–02 点已清除
   - `GET /hourly-usage?meter_id=meter-east-01` — 00–02 点的小时电量、`billable`、`reason` 原样可查
9. 再发一次同样的请求体 — `idempotent_retry=true`、`readings_deleted=0`，不重复清理
10. 改请求体为 `{"meter_id":"meter-east-01","cutoff_second":1793498400}`（02:00，更早）— HTTP 409，截止时刻只能前移
11. `POST /batches` 上传 `samples/batch7_reading_before_seal_cutoff.csv` — 含 02:00 的早于截止时刻读数，整批 HTTP 409 拒批；再查读数与小时结果均不变
12. 上传 `samples/batch8_backfill_after_seal.csv` — 06:00/07:00 的截止后补传正常入库，新小时照常核算；封存前的小时结果保持不变
13. `POST /seals` 请求体 `{"meter_id":"meter-east-01","cutoff_second":1793512800}`（06:00）— 截止时刻前移，03–05 点原始读数被清除，全部小时电量仍可查

## 并发与一致性设计

两个容器（`api-a`、`api-b`）挂载同一个 named volume，共享 `/data/energy.db`：

- **WAL 模式 + `busy_timeout=30s`**：读写不互斥，写者排队而不是报 `database is locked`
- **每批 / 每次封存一个 `BEGIN IMMEDIATE` 事务**：批次行、原始读数、重算的小时电量，
  或封存位置、被清除的原始读数，都在同一事务里一起提交或一起回滚。
  写者被串行化，因此两个实例同时补传 / 封存同一表时，清理、封存位置与小时结果共同生效、不会互相覆盖
- **封存边界在写事务内检查**：补传批次在同一事务里读取 `seals` 截止时刻，
  含早于截止时刻读数的批次整批回滚；并发封存无法把边界移到批次检查的"身后"
- **重算窗口钳制**：封存后 `recompute_window` 的删除下界不低于截止时刻，
  截止前的小时行即使原始读数已清除也永不被删除或重算
- **查询只读已提交快照**：不会看到"新读数配旧用量"或"读数已清、封存未登记"的中间态
- **校验失败（422）或冲突（409）发生在提交前**：此前完整结果照常可查，失败操作不留痕迹
- `(meter_id, utc_second)` 与 `seals.meter_id` 主键兜底，同值重传与同截止时刻重试天然幂等

## 项目结构

```
app/
  main.py    # FastAPI 路由与请求校验（/batches、/seals、查询）
  logic.py   # CSV 解析、入库事务、小时电量窗口重算、封存事务
  db.py      # SQLite 连接（WAL、busy_timeout）与建表（含 seals）
samples/     # 本地样例 CSV（缺口/迟到填洞/重传/冲突/回退/并发/封存拒批/封存后补传）
scripts/demo.sh   # 端到端演示：跑通全部业务场景含封存全流程
tests/test_app.py # 31 个 pytest 用例（含多线程并发补传、并发封存与封存规则）
docker-compose.yml # api-a:8001 + api-b:8002 共享 named volume
```
