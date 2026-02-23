# Docker 长期展示部署说明

本目录使用 `deploy/.env` 作为唯一配置源。

## 1. 配置文件

编辑 `deploy/.env`：

- `IMAGE_REPO`：镜像仓库前缀（本机镜像可用 `local`）
- `IMAGE_TAG`：版本号（例如 `v0.3.0`）
- `BACKEND_PORT`：后端 WebSocket 暴露端口
- `FRONTEND_PORT`：前端 HTTP 暴露端口

## 2. 管理命令

统一使用脚本（已处理 `docker-compose` 的 Python 依赖冲突）：

```bash
deploy/compose.sh up
deploy/compose.sh ps
deploy/compose.sh logs
deploy/compose.sh restart
deploy/compose.sh down
```

## 3. 升级版本

1. 构建/拉取新镜像（tag 与 `IMAGE_TAG` 对应）  
2. 修改 `deploy/.env` 的 `IMAGE_TAG`  
3. 执行：

```bash
deploy/compose.sh up
```

容器会按新镜像自动重建并保持 `restart: unless-stopped`。
