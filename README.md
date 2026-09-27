# 车辆安全召回与修复跟踪系统

标准库 Python 3.11+ + SQLite。支持召回草稿、监管审核发布、范围按版本调整、车辆登记与跨境流转、维修网点零件库存、修复证据复核、未完成高风险车辆统计、通知和监管上报版本，以及**网点垫付赔付申请**（确认即生成、按完成时口径核价、监管确认前可重核、驳回可更正重提、防重复赔付）。

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
- `POST /api/repairs`、`POST /api/repairs/{id}/review`：报告并复核维修。
- `POST /api/repairs/{id}/documents`：网点更正已确认维修单的单据（会触发重核价；监管已确认后禁止）。
- `GET /api/compensation/standards`、`POST /api/compensation/standards`：查询/维护赔付口径（方案版本+车型+国家 → 金额）。
- `GET /api/compensation/ledger`：按网点汇总可赔/待核/争议金额与明细（支持 `?dealer_id=`）。
- `GET /api/compensation/claims/{id}`：赔付申请明细（含追加式账页）。
- `POST /api/compensation/claims/{id}/review`：监管确认通过/驳回（驳回必填原因）。
- `POST /api/compensation/claims/{id}/resubmit`：网点对驳回申请更正重提（可带新单据哈希，按当前口径重核价）。
- `POST /api/compensation/claims/{id}/settle`：厂家对已确认申请付款（一次性，防重复）。
- 页面：`/`（状态总览）、`/claims`（网点赔付台账：可赔/待核/争议金额 + 明细 + 账页）。
- `GET /api/recalls/{id}/unfinished`：查看高风险未完成车辆。
- `GET /api/state`、`GET /api/health`：状态与健康检查。

## 赔付模块边界

三个关注点分别维护，互不耦合：

- `pricing.py` — **赔付口径**：按 `(方案版本, 车型, 国家)` 配置标准额；不持有申请状态。
- `ledger.py` — **账本**：一笔已确认维修单对应唯一赔付申请（`repair_id UNIQUE`），追加式 `claim_revisions` 账页保留每次核价、驳回原因与旧金额；不提供口径编辑。
- `static/claims.html` — **页面**：只消费读接口，按网点展示可赔（已确认，含已付/待付）、待核（确认前）、争议（驳回）金额和明细。
- `app.py` 只做编排：维修确认→生成申请；口径变更→待核申请重核价；单据变更→单笔重核价。

规则：维修确认时按**完成时**的方案版本、车型、车辆所在国家核价并留存；监管确认前口径或单据变化自动重新核价；监管确认后金额锁定；驳回后网点可更正重提，旧金额与原因只追加不覆盖；付款一次性，同一维修单不能拿两次钱。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前为本地原型：跨境规则用许可字符串模拟，零件库存与维修记录是简化模型，不包含真实 VIN 解码、监管接口、物流系统或法定通知渠道。
