# Monitor 前端联调开发计划（Git Flow 可追踪）

## 1. 目标与约束
- 目标：在 `dynamic-topo` 前端中对接 monitor 数据契约并形成可直观看板能力。
- 契约基准：`/home/zyren/monitor/docs/data_contract_by_dynamic_topo.md`（外部只读参考）。
- 流程约束：严格执行 `Issue -> feature 分支 -> PR -> merge develop`，保证一事一分支、一事一 PR。

## 2. Issue 与分支拆分

| Issue | 目标 | 分支名 | 交付物 |
|---|---|---|---|
| I-029 | API 封装与类型定义 | `feature/I-029-monitor-api-contract-v1` | `frontend/src/api/*`、类型定义、统一响应处理 |
| I-030 | 展示映射层与状态管理 | `feature/I-030-monitor-visual-mapping-v1` | 字段到 UI ViewModel 映射、阈值规则、筛选状态 |
| I-031 | 监控看板 UI v1 | `feature/I-031-monitor-dashboard-v1` | 总览区、拓扑联动区、告警列表与详情区 |
| I-032 | 测试与文档收口 | `feature/I-032-monitor-contract-tests-docs` | 单测/联调脚本、字段对照表、回归清单 |

## 3. 每个 Issue 的强制执行步骤
1. 在平台创建/领取 Issue，写明目标、范围、验收标准、参考契约路径。
2. 从 `develop` 创建分支：`git checkout -b feature/I-XXX-...`.
3. 小步提交，commit 必含 Issue 编号。
4. 首次提交后立刻推送远端：`git push -u origin <branch>`.
5. 发起到 `develop` 的 PR，标题格式：`[I-XXX] 中文标题`。
6. 在 PR 中补齐：验证命令、截图或日志、风险与回滚。
7. 合并后关闭 Issue，并记录 PR 链接与最终 commit。

## 4. Commit 规范
- 格式：`<type>(<module>): [I-XXX] 中文描述`
- 示例：
  - `feat(frontend-api): [I-029] 新增 monitor ingest 客户端与错误映射`
  - `feat(frontend-view): [I-031] 新增告警时间线与拓扑联动定位`
  - `test(frontend): [I-032] 增加字段映射与状态阈值测试`

## 5. PR 检查清单（必填）
- [ ] 关联单一 Issue，且 PR 仅覆盖该 Issue 范围
- [ ] 引用契约文档路径并说明是否有偏差
- [ ] 提供验证证据（命令输出/截图/录屏）
- [ ] 提供风险与回滚方案
- [ ] 更新相关文档（映射表、使用方式、已知限制）

## 6. 验收标准（含“直观展示”）
- [ ] 首屏可同时看到健康概览、关键告警数、拓扑异常态。
- [ ] 任意告警可在 2 次交互内定位到节点或链路对象。
- [ ] 指标颜色语义一致：正常/预警/严重，不出现同色异义。
- [ ] 字段展示与契约一致，缺失字段有明确降级显示。
- [ ] 不影响现有拓扑实时渲染性能与交互流畅性。

## 7. 风险与回滚策略
- 风险：契约字段新增导致前端解析失败。
- 处理：解析层采用“可选字段 + 默认值 + 告警日志”策略，避免页面崩溃。
- 回滚：按 Issue 粒度回滚 feature 分支；合并后通过 revert 对应 PR。

## 8. 实施顺序
1. 先完成 I-029（底层稳定）。
2. 再完成 I-030（映射与规则）。
3. 再完成 I-031（界面呈现与交互）。
4. 最后 I-032（测试与文档封板）。
