# Monitor 联调规范：前端故障注入闭环（v1）

更新时间：2026-02-26  
适用范围：`dynamic-topo` 前端 + `monitor-collector` 联调  
目标：用前端“注入故障”能力形成闭环，验证 monitor 能正确抓取、落库、分析并回显。

## 1. 闭环目标

单次闭环应满足：

1. 前端注入故障（节点或链路）成功。
2. monitor 侧可观测到故障事件并映射到正确对象。
3. analysis/simulation 结果发生可解释变化。
4. 前端解除故障后，monitor 侧状态恢复。

## 2. 前端真实控制协议（冻结）

前端通过 WebSocket 向 `dynamic-topo` 后端发送控制消息：

- WS 地址：`ws://<topo-backend-host>:8765`
- 请求格式：
  - 公共字段：`action`, `request_id`

### 2.1 注入节点故障

请求：
```json
{
  "action": "inject_node_fault",
  "request_id": "req-<ts>",
  "node_id": "SAT-INCL-001"
}
```

### 2.2 注入链路故障

请求：
```json
{
  "action": "inject_link_fault",
  "request_id": "req-<ts>",
  "a": "SAT-INCL-001",
  "b": "SAT-INCL-011"
}
```

### 2.3 解除故障

单条解除：
```json
{
  "action": "clear_fault",
  "request_id": "req-<ts>",
  "fault_id": "fault-xxxx"
}
```

全部解除：
```json
{
  "action": "clear_all_faults",
  "request_id": "req-<ts>"
}
```

### 2.4 查询当前故障列表

```json
{
  "action": "list_faults",
  "request_id": "req-<ts>"
}
```

## 3. 后端回执格式（monitor 必须识别）

`dynamic-topo` 控制回执统一为：

```json
{
  "type": "control_ack",
  "ok": true,
  "action": "inject_link_fault",
  "request_id": "req-xxx",
  "deduplicated": false,
  "fault": {
    "fault_id": "fault-xxxxx",
    "fault_type": "INTERRUPTED",
    "target": {"a": "SAT-INCL-001", "b": "SAT-INCL-011"},
    "created_at": "2026-02-26T08:00:00+00:00"
  },
  "faults": [...]
}
```

失败时：
- `ok=false`
- `error` 描述错误原因

## 4. 帧级状态信号（monitor 二次校验）

拓扑帧 `metrics` 中有故障计数，可用于校验注入是否生效：

- `metrics.fault_node_count`
- `metrics.fault_link_count`

预期：
- 注入节点故障后 `fault_node_count` 增加
- 注入链路故障后 `fault_link_count` 增加
- 清除后计数回落

## 5. monitor 侧采集与映射要求

### 5.1 最低采集要求

monitor 至少采集两类信息：

1. `control_ack`（事件流）
2. 帧 `metrics`（状态流）

### 5.2 映射规则（建议）

1. `fault_type=DAMAGED`
  - `scope_type=node`
  - `scope_id=target.node_id`
  - 严重级别建议：`critical`

2. `fault_type=INTERRUPTED`
  - `scope_type=link`
  - `scope_id=target.a<->target.b`（按字典序标准化）
  - 严重级别建议：`critical`

3. 去重
  - 若 `deduplicated=true`，不重复计入新告警，只更新观察时间。

4. 恢复
  - `clear_fault/clear_all_faults` 后，对应告警应进入恢复态或关闭态。

## 6. 与 analysis/simulation 的闭环验证

在 monitor 联调中，至少跑以下三组：

1. 节点故障闭环
  - 注入 `DAMAGED(node)`
  - 调 `POST /api/v1/bff/analysis/run`（`focused/node`）
  - 验证 `summary.risk_level` 升高、`topology_impact.impacted_nodes` 含该节点
  - 清除故障后再次分析，风险回落

2. 链路故障闭环
  - 注入 `INTERRUPTED(link)`
  - 调 `POST /api/v1/bff/analysis/run`（`focused/link`）
  - 验证 `topology_impact.impacted_links` 命中该链路
  - 清除故障后命中消失

3. 推演闭环
  - 用同一链路调用：
    - `POST /api/v1/bff/simulation/create`
    - `POST /api/v1/bff/simulation/{id}/step` 直至 completed
    - `GET /api/v1/bff/simulation/{id}/timeline`
  - timeline 非空，且风险曲线与故障状态一致

## 7. 前后端对象命名一致性（必须）

1. 节点：使用 topo 节点 ID（如 `SAT-INCL-001`）
2. 链路：统一 `A<->B`（字典序）
3. `topology_epoch`：联调固定 `1708848000`

不满足命名一致性会直接导致：
- `INVALID_SCOPE`
- 前端“定位失败”或“分析对象为空”

## 8. 验收标准

1. 注入/解除回执 `ok=true`，错误率 0。
2. monitor 能在 2 秒内观测到注入事件（事件流或状态流）。
3. `analysis/run` 三入口均 200：`focused(node/link)` + `global(network)`。
4. `simulation` 最小流程可完成，`timeline` 非空。
5. 整体 60 秒连续联调无 5xx。

## 9. 问题上报模板（monitor）

- 注入操作：`inject_node_fault` / `inject_link_fault` / `clear_*`
- request_id：
- fault_id：
- 观测到的 control_ack：
- 观测到的帧计数变化：`fault_node_count/fault_link_count`
- analysis/run 请求与响应：
- simulation 请求与响应：
- 首次发生时间（UTC+8）：
- 复现概率：
