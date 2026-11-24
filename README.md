# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。

## 锁账（充日封印）与开充争用

并发口径**全局唯一**：**锁账世代优先**。封印与开充都在 `BEGIN IMMEDIATE`
写事务里执行，两者在数据库层严格串行，因此只有两种可重放结局：

| 结局 | 封印 | 开充 | 结果 |
| --- | --- | --- | --- |
| 开充先提交 | 200，世代+1，世代事件带上该会话 | 200，落会话/占用/序号/回执 | 充电中 +1，回执与占用同命运 |
| 封印先提交 | 200，世代+1 | **409 整笔让路** | 无会话、无占用、序号不动、无回执 |

- 半截（事务内任意点抛错或进程崩溃）一律 `ROLLBACK` 回争用前快照：世代事件、
  会话行、桩/枪占用、会话序号、开充回执、账本序号不会分叉。
- 封印/解封可带 `预期世代`，世代已被并发推进则返回 409，避免覆盖。
- 单一读口径 `GET /api/充日/闸` 供开充闸与锁账徽标共用；`GET /api/总览`
  的充电中桶与 `充日已封印`/`封印世代` 取自同一快照。
- 可重放账本：`GET /api/充日/账本`（封印世代事件与开充回执按同一账本序号合流）。

前端「充日封印」页展示世代事件、开充回执与争用账本；「开会话」页显示开充闸状态。

### 测试

```powershell
cd backend
.\.venv\Scripts\pip install pytest httpx
.\.venv\Scripts\python -m pytest tests/ -q
```

夹具覆盖：封印先成 / 开充先成 / 双半截回滚、序号已加会话行回滚、
开充回执成功必有占用、脉冲不误进已回滚会话、总览充电中与锁账徽标长期一致。
