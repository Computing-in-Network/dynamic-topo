# 发布检查清单（v0.2.0）

## 1. 分支与提交
- `develop` 工作区干净：`git status --short` 为空。
- 发布前文档与代码均已通过评审并合并到 `develop`。

## 2. 回归测试（必须）
- 后端：
  - `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run python -m pytest -q`
- 前端：
  - `cd frontend && npm run build`

## 3. 版本文档（必须）
- `CHANGELOG.md` 已包含本次版本变更摘要。
- `docs/releases/v0.2.0.md` 已包含发布说明。
- `README.md` 已包含发布相关入口链接。

## 4. 发布流程（Git Flow）
1. 创建发布 PR：`develop -> main`
2. 合并发布 PR 到 `main`
3. 在 `main` 打发布标签（示例）：
   - `git checkout main`
   - `git pull --ff-only origin main`
   - `git tag -a v0.2.0 -m "release: v0.2.0"`
   - `git push origin v0.2.0`

## 5. 回滚预案（必须）
- develop 快照回滚：使用最近 `rb-develop-*` 标签。
- 正式版本回滚：使用上一稳定 `vX.Y.Z` 标签。
- 参考：`docs/gitflow_rollback.md`

## 6. 发布后验证（建议）
- 后端 ws 服务可连接，指标字段完整。
- 前端核心功能可用：
  - 图层控制
  - 节点/链路拾取侧栏
  - 告警显示
  - 时间控制
