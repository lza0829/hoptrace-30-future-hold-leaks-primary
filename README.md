# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 未来点暂缓（future_ts_hold）

`event_ts` 晚于可配置业务钟的轨迹点不写正式时间线，先落 `future_holds` 暂缓表；
业务钟走到后经显式晋升或到达扫描才入正式线并重算投影。业务钟回拨不回溯改写已正式点。

**双钟同源**：装载闸与晋升扫描都只读 `clock_config` 的同一份业务钟读数，绝不使用墙钟。
每个业务钟版本在 `clock_readings` 留一条不可变读数；晋升批次把扫描所用读数冻结成快照
（`promotion_batches.clock_ts/clock_version`、`future_holds.promoted_clock_ts`），
之后业务钟怎么回拨都不能改写已晋升点。任一点在任一时刻只在一侧可见：
要么正式线，要么暂缓抽屉。

| 接口 | 说明 |
| --- | --- |
| `GET/POST /api/clock` | 读写业务钟（`advance=true` 只许向前） |
| `GET /api/holds?status=held` | 暂缓集合 |
| `POST /api/holds/promote` | 显式晋升（传 `keys`）或到达扫描（不传）；可带 `expected_clock_version` / `expected_clock_ts` 绑定扫描口径 |
| `GET /api/hold-proofs?batch_id=&clock_version=` | 暂缓证明，按批次或钟版本可查 |
| `GET /api/promotion-batches?clock_version=` | 晋升批次不可变钟读数快照 |
| `GET /api/clock-audit?waybill_id=` | 双钟核证（只读）：逐点核对装载钟/晋升钟同源与互斥可见 |

前端「暂缓」页可设定业务钟、看暂缓集合、触发晋升、查暂缓证明、晋升批次与逐点双钟核证；
「证明」页展示投影证明与核证结论，「测绘板」检查器显示当前运单的核证状态。
