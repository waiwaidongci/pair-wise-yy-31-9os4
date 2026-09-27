# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计，以及通知和监管上报版本。

维修确认后自动生成赔付申请：按完成时的方案版本、车型和维修网点所在国家留存赔付口径；监管确认前口径或维修单据变化会重新核价，确认后金额锁定；同一维修单只对应一笔账，驳回可更正重提且旧金额与原因留痕。金额一律以分为单位（`amount_cents`）。

## 模块划分（赔付口径、账本、页面分开维护）

- `standards.py`：赔付口径。按（方案版本, 车型, 国家）维护标准，调整只追加新版本，旧版本保留。
- `ledger.py`：赔付账本。申请生成、重新核价、审批、驳回重提、支付（仅 `approved → paid` 原子迁移，防止同一维修单重复拿钱），每次变动追加 `claim_versions` 历史。
- `static/claims.html`：赔付台账页（`/claims`），按网点显示可赔、待核、争议、已付金额和明细。
- `app.py`：召回主流程与 HTTP 路由，通过钩子（`claim_hook`/`evidence_hook`）与账本联动。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

默认端口 `8213`。身份通过 `X-Actor` 与 `X-Role` 请求头模拟，角色为 `manufacturer`、`regulator`、`dealer`。可用 `--port`、`--db` 覆盖。

## 主要接口

- `POST /api/dealers`、`POST /api/vehicles`：登记网点和车辆。
- `POST /api/vehicles/{vin}/transfer`：更新车辆所在国家和车主。
- `POST /api/recalls`、`POST /api/recalls/{id}/submit`：创建并提交召回。
- `POST /api/recalls/{id}/review`：监管发布或退回。
- `POST /api/recalls/{id}/scope`：调整召回范围并生成新版本通知/上报。
- `POST /api/recalls/{id}/parts`：维修网点入库。
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修（确认后自动生成赔付申请）。
- `POST /api/repairs/{id}/evidence`：网点更正维修单据，触发未确认申请重新核价。
- `POST /api/standards`、`GET /api/standards`：维护与查看赔付口径（按方案版本+车型+国家，追加新版本）。
- `GET /api/claims`、`GET /api/claims/{id}`：赔付申请列表与详情（含金额/原因历史）。
- `GET /api/claims/summary`：按网点汇总可赔、待核、争议、已付金额与明细。
- `POST /api/claims/{id}/review`：监管确认（`approve`）或驳回（`reject`，必填原因）。
- `POST /api/claims/{id}/resubmit`：网点驳回后更正重提，按当前口径重核。
- `POST /api/claims/{id}/pay`：支付已确认申请，同一维修单只会支付一次。
- `GET /claims`：赔付台账页面。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可字符串模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
