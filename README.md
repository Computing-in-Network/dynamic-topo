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

## 测试

```bash
uv run python -m pytest -q
```
