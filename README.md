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

这个文件需要修改的