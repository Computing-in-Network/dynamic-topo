# dynamic-topo

300 节点动态拓扑仿真项目，包含后端实时计算和前端 3D 可视化。

## 已实现能力

- 300 节点组成：L1(100) + L2(100) + A1(50) + S1(50)
- 1Hz 动态推进（可配置）
- 基础可用链路判定：
  - LoS 地球遮挡
  - 按节点类型的距离门限
  - 卫星波束覆盖（nadir 锥形）
  - 节点容量约束（最大邻居数、卫星波束槽位）
  - 上下线 hysteresis（抗抖动）
- Redis 写入：`node:pos`（Hash）与 `topo:adjacency`（Stream bitmap）
- WebSocket 实时帧推流：节点经纬高、链路、统计指标
- 统计指标包含连通分量、最大分量占比、直径近似等连通性信息
- Cesium 前端：地球、节点运动、轨迹、实时链路

## 后端运行

```bash
uv sync --dev
uv run python main.py --steps 5 --dt 1.0
```

运行 WebSocket 推流服务：

```bash
uv run python -m dynamic_topo.stream_server --host 0.0.0.0 --port 8765 --dt 1.0
```

链路策略参数外置（JSON）：

```bash
uv run python -m dynamic_topo.stream_server \
  --link-policy docs/link_policy.example.json \
  --hot-reload-link-policy
```

- `--link-policy`：加载链路策略 JSON（覆盖默认阈值与容量参数）
- `--hot-reload-link-policy`：运行中检测文件变更并自动重载
- 可选稳定性参数：`min_link_up_s`、`min_link_down_s`（链路上线/下线后的最短保持时长）
- 可选增量几何参数：`incremental_geometry`、`incremental_move_threshold_m`、`incremental_rebuild_ratio`

## 前端运行

```bash
cd frontend
npm install
npm run dev
```

默认连接 `ws://localhost:8765`。如需改地址：

```bash
VITE_TOPO_WS_URL=ws://<your-host>:8765 npm run dev
```

监控面板默认请求 `http://localhost:9010/api/v1/monitor/snapshot`。可按需覆盖：

```bash
VITE_MONITOR_BASE_URL=http://<collector-host>:9010 npm run dev
```

可选参数：
- `VITE_MONITOR_TOPOLOGY_EPOCH`：只拉取指定 `topology_epoch`
- `VITE_MONITOR_POLL_MS`：快照轮询间隔（毫秒，最小 1000）

## 测试

```bash
uv run python -m pytest -q
```

故障注入相关测试（后端模型 + WS 控制通道）：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest tests/test_topology.py tests/test_stream_server.py -q
```

更新拓扑回归快照基线：

```bash
uv run python scripts/generate_topology_snapshot.py --output tests/fixtures/topology_snapshot.json
```

基于实时拓扑增量下发静态路由（300 容器）：

```bash
# 先演练，不实际写路由
uv run python scripts/push_static_routes.py \
  --ws-url ws://127.0.0.1:8765 \
  --mapping-csv docs/node_mapping_300.csv \
  --container-ip-source docker \
  --min-stable-frames 2 \
  --dry-run

# 实际下发
uv run python scripts/push_static_routes.py \
  --ws-url ws://127.0.0.1:8765 \
  --mapping-csv docs/node_mapping_300.csv \
  --container-ip-source docker \
  --min-stable-frames 2
```

- 路由为每节点 `/32` 目的前缀（默认 `10.200.0.0/16` 生成）
- 仅做增量更新（`ip route replace` + 必要删除），避免每帧全量重灌
- 容器 IP 可来自 CSV 扩展字段（`container_ip` 等）或 `docker inspect`
- 当 `container_name` 失配时，脚本会自动尝试 `container_id`（若 CSV 提供）

基于实时拓扑下发 `erv300_sim` 二层策略（`/opt/sim/policy.json`）：

```bash
# 演练：仅生成策略并输出摘要，不写入仿真器
uv run python scripts/push_sim_policy.py \
  --ws-url ws://127.0.0.1:8765 \
  --mapping-csv docs/node_mapping_300.csv \
  --sim-container erv300_sim \
  --sim-policy-path /opt/sim/policy.json \
  --min-stable-frames 2 \
  --dry-run --once

# 实际下发：写入 policy.json 并向 l2_center_sim.py 发送 HUP 热重载
uv run python scripts/push_sim_policy.py \
  --ws-url ws://127.0.0.1:8765 \
  --mapping-csv docs/node_mapping_300.csv \
  --sim-container erv300_sim \
  --sim-policy-path /opt/sim/policy.json \
  --min-stable-frames 2 \
  --once
```

- 规则由实时 `links` 生成：每条边生成双向 `unicast/forward`
- 通过 `veth_0` MAC 做节点映射，避免依赖容器 IP
- 默认禁用 WS 代理（可加 `--respect-proxy` 覆盖）

## Git Flow 回退规范

- 详见：`docs/gitflow_rollback.md`
- 每次 `feature -> develop` 合并后请打回退标签：

```bash
./scripts/create_rollback_tag.sh <issue_number>
git push origin <tag_name>
```

## 发布文档

- 变更日志：`CHANGELOG.md`
- 发布说明：`docs/releases/v0.2.0.md`
- 发布说明（最新）：`docs/releases/v0.3.0.md`
- 发布检查清单：`docs/release_checklist.md`
- 故障注入验收手册：`docs/fault_injection_acceptance.md`

## 故障注入（当前能力）

- 节点故障：`DAMAGED`（节点相关链路全部失效）
- 链路故障：`INTERRUPTED`（指定链路强制断开）
- 前端操作：
  - 选中节点 -> `注入节点故障`
  - 选中链路 -> `注入链路故障`
  - 右侧故障列表 -> `解除该故障` / `解除全部故障`
- 后端原则：人工故障优先级高于自动拓扑计算

## 使用 Gitea Actions + Docker 部署（一步步）

1. 准备部署机（只做一次）
   - 安装 `docker` 和 `docker compose`
   - 新建目录，例如 `/opt/dynamic-topo`
   - 把 `deploy/docker-compose.prod.yml` 放到部署目录

2. 准备镜像仓库（只做一次）
   - 可用 Gitea Container Registry 或私有 Docker Registry
   - 记录仓库地址、用户名、密码

3. 在 Gitea 仓库配置 Secrets（仓库 -> Settings -> Secrets）
   - `REGISTRY`: 仓库地址（如 `registry.example.com`）
   - `REGISTRY_USER`: 仓库用户名
   - `REGISTRY_PASSWORD`: 仓库密码/令牌
   - `IMAGE_NAMESPACE`: 镜像命名空间（如 `team`）
   - `DEPLOY_HOST`: 部署机 IP/域名
   - `DEPLOY_USER`: 部署机 SSH 用户
   - `DEPLOY_SSH_KEY`: 私钥内容（建议 ed25519）
   - `DEPLOY_PATH`: 部署目录（如 `/opt/dynamic-topo`）

4. 启用并运行 workflow
   - Workflow 文件：`.gitea/workflows/deploy.yml`
   - 推送 `main` 分支会自动触发
   - 或在 Gitea Actions 页面手动 `Run workflow`

5. 部署验证
   - 前端：`http://<DEPLOY_HOST>:8080`
   - 后端 WS：`ws://<DEPLOY_HOST>:8765`
   - 在部署机查看容器：`docker ps`
