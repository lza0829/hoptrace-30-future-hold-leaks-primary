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

| 接口 | 说明 |
| --- | --- |
| `GET/POST /api/clock` | 读写业务钟（`advance=true` 只许向前） |
| `GET /api/holds?status=held` | 暂缓集合 |
| `POST /api/holds/promote` | 显式晋升（传 `keys`）或到达扫描（不传） |
| `GET /api/hold-proofs?batch_id=&clock_version=` | 暂缓证明，按批次或钟版本可查 |

前端「暂缓」页可设定业务钟、看暂缓集合、触发晋升、查暂缓证明。
