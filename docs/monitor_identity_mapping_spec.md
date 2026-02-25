# Monitor 与 Docker 网络模拟器标识一致性规范（V1）

版本：v1.0  
日期：2026-02-25  
适用范围：`dynamic-topo`、`monitor collector`、前端可视化联调

## 1. 目标
- 保证前端展示、告警定位、链路关联与 Docker 网络模拟器中的真实容器一一对应。
- 避免因别名（如 `N-001`）与容器名不一致导致“定位失败”。

## 2. 标识模型

### 2.1 节点唯一标识（Canonical）
- `node_uid`：全链路唯一主键，**V1 固定使用 `docker_name`**。

### 2.2 节点属性（非主键）
- `docker_name`：容器名（等于 `node_uid`）
- `docker_ip`：容器当前 IP（可变）
- `container_id`：容器 ID（可选）
- `topo_node_id`：拓扑内部编号（如 `N-001`，可选别名）
- `topology_epoch`：拓扑批次

### 2.3 链路唯一标识（Canonical）
- `link_uid = sort(node_uid_a, node_uid_b).join("<->")`
- 要求无向链路统一排序，避免 `A->B` 与 `B->A` 重复。

## 3. 映射表规范

### 3.1 权威映射表（必须）
按 `topology_epoch` 维护如下结构：

```json
{
  "topology_epoch": 1708848000,
  "nodes": [
    {
      "node_uid": "uav_01",
      "docker_name": "uav_01",
      "docker_ip": "10.10.0.12",
      "container_id": "ab12cd34",
      "topo_node_id": "N-001"
    }
  ]
}
```

### 3.2 一致性约束
- 同一 `topology_epoch` 内 `node_uid` 唯一。
- 同一 `topology_epoch` 内 `docker_name` 唯一。
- `node_uid == docker_name`（V1 强约束）。
- `docker_ip` 允许变化，但变更必须写入最新映射快照。

## 4. 数据契约扩展要求

## 4.1 node_metric 必填新增
- `node_uid`（推荐与 `node_id` 同时携带，V1 渐进迁移）
- `docker_name`
- `docker_ip`（可选但推荐）

## 4.2 link_metric 必填新增
- `src_node_uid`
- `dst_node_uid`
- `link_uid`（按规范生成）

## 4.3 alarm 推荐新增
- `scope_uid`（节点告警填 `node_uid`；链路告警填 `link_uid`）
- `scope_id` 继续保留用于兼容展示

## 5. Collector 入站校验

## 5.1 校验顺序
1. 按 `topology_epoch` 读取映射表。
2. 校验 `node_uid/link_uid` 是否存在。
3. 若仅有旧字段（如 `node_id=N-001`），通过映射表反查 `node_uid`。
4. 校验通过后再入库/发布。

## 5.2 校验失败处理
- 记录错误并返回标准错误码，不得静默吞掉：
  - `UNKNOWN_NODE_UID`
  - `UNKNOWN_LINK_UID`
  - `EPOCH_MAPPING_NOT_FOUND`
  - `INCONSISTENT_NODE_ALIAS`

## 6. 前端展示与定位规则
- 前端内部定位只使用 `node_uid/link_uid`。
- UI 显示建议：`docker_name (docker_ip)`。
- 告警定位优先使用 `scope_uid`；无 `scope_uid` 时回退 `scope_id` + 映射解析。
- 映射缺失时必须显示明确反馈：`定位失败：映射不存在`。

## 7. 漂移检测与更新
- 周期性拉取 Docker 实际容器清单（`docker_name/docker_ip/container_id`）。
- 与当前映射表 diff：
  - 新增容器：新增映射
  - 消失容器：标记失效
  - IP 变化：更新 `docker_ip`
- 变更后递增 `snapshot_version` 并广播给前端。

## 8. 迁移计划（V1 -> V2）
1. V1 兼容期：同时接受 `node_id/link_id` 与 `node_uid/link_uid`。
2. V1.1：前端与告警链路全部切到 `*_uid`。
3. V2：`node_id/link_id` 降级为展示字段，不再用于定位主键。

## 9. 验收标准
- 任意告警点击定位成功率 >= 99%（映射完整条件下）。
- `scope_uid` 与拓扑对象匹配正确率 100%。
- 容器 IP 漂移后 30s 内映射可更新并恢复定位。
