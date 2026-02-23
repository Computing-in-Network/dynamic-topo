# 故障注入验收手册

适用版本：包含 issue `#36`、`#37`、`#39` 相关改动后的 `develop` 分支。

## 1. 启动方式

1. 启动后端：

```bash
uv run python -m dynamic_topo.stream_server --host 0.0.0.0 --port 8765 --dt 1.0
```

2. 启动前端：

```bash
cd frontend
npm install
npm run dev
```

3. 浏览器打开 `http://localhost:5173/`。

## 2. 验收项

### 2.1 节点故障注入

1. 在 3D 视图点击任意节点（卫星/飞机/船只）。
2. 在详情侧栏点击 `注入节点故障`。
3. 期望结果：
   - 节点变为故障样式（红色）。
   - 该节点相关链路在 1 tick 内断开。
   - 指标区 `fault nodes` 增加。
   - 故障列表出现一条 `DAMAGED` 记录。

### 2.2 链路故障注入

1. 点击任意可见链路。
2. 在详情侧栏点击 `注入链路故障`。
3. 期望结果：
   - 该链路在 1 tick 内断开。
   - 指标区 `fault links` 增加。
   - 故障列表出现一条 `INTERRUPTED` 记录。

### 2.3 解除单条故障

1. 在故障列表中点击某条 `解除该故障`。
2. 期望结果：
   - 故障列表移除该条记录。
   - 对应节点/链路恢复到自动计算结果（允许存在 1 tick 刷新延迟）。

### 2.4 解除全部故障

1. 点击 `解除全部故障`。
2. 期望结果：
   - 故障列表为空。
   - `fault nodes` 与 `fault links` 归零。
   - 拓扑回到无人工故障状态。

### 2.5 重复注入去重

1. 对同一节点连续点击两次 `注入节点故障`。
2. 期望结果：
   - 故障列表仅保留一条对应记录（去重生效）。
   - 控制状态提示去重。

## 3. 自动化回归命令

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest tests/test_topology.py tests/test_stream_server.py -q
cd frontend && npm run build
```

## 4. 问题定位建议

- 若注入按钮无响应：
  - 检查前端 `control` 状态文本是否报错。
  - 检查浏览器 console 的 WebSocket 连接状态。
- 若故障未生效：
  - 确认后端运行的是最新 `develop`。
  - 检查后端日志是否存在控制命令解析错误。
