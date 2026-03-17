# 星历预测路由第一版

## 背景

当前仓库已经具备：

- `dynamic_topo` 按时间步实时计算拓扑
- `push_sim_policy.py` 将当前链路关系下发到 `star300lite_sim`
- `push_static_routes.py` 将当前连通图转换为节点静态路由

但当前路由仍是“看到当前拓扑后立即重算并下发”的反应式模式，还没有利用星历可预测这一特性。

对于卫星场景，更合理的第一步是：

1. 提前计算未来一段时间的拓扑变化
2. 预生成未来时间窗口内的路由计划
3. 为后续“定时切换路由”“减少重算抖动”“前端展示未来路径变化”提供统一数据结构

## 目标

1. 新增星历预测路由第一版能力
2. 基于 `TopologyEngine` 直接生成未来时间窗口的路由计划
3. 将相同路由状态压缩成时间段，形成可复用的 route plan
4. 提供一个按计划应用的脚本入口，支持后续接入容器路由下发

## 范围

1. 新增预测路由计划生成脚本
   - 输入未来时间窗口、采样间隔、节点映射
   - 输出未来每个时间段的连通图和路由计划
   - 对连续相同计划做压缩

2. 新增预测路由计划应用脚本
   - 读取 route plan
   - 支持按计划选择某个时间段并应用对应静态路由
   - 第一版允许先做 one-shot / dry-run / 相对时间驱动

3. 文档补充
   - 如何生成 route plan
   - 如何查看和应用 route plan
   - 当前第一版的边界

4. 预测 sim policy 与定时切换
   - 新增未来时间窗口的 sim policy plan
   - 支持选择某个 slot 应用到 `star300lite_sim`
   - 支持按时间偏移自动切换 route slot 和 sim policy slot

## 验收标准

1. 能生成未来时间窗口的 `route_plan.json`
2. `route_plan.json` 至少包含：
   - 规划起止时间
   - 采样间隔
   - 每个时间段的边数
   - 每个时间段的组件规模
   - 每个时间段的按节点路由下一跳
3. 能基于生成结果选择一个时间段执行一次路由应用或 dry-run
4. 文档说明清楚第一版与当前实时静态路由控制器的关系
5. 能生成未来时间窗口的 `predictive_sim_policy_plan.json`
6. 能按时间偏移自动切换 route slot 与 sim policy slot

## 注意事项

1. 第一版目标是“预测计划生成 + 计划应用入口”，不是一次性替换现有实时控制器
2. 计划路由若要用于持续生产切换，还需要后续解决时钟、切换窗口、失败回退等问题
3. 若未来拓扑本身存在长期分裂，预测路由也只能如实反映不可达，而不能凭空保证全连通

## 当前实现进展

已完成：

- `build_predictive_route_plan.py`
- `apply_predictive_route_plan.py`
- `build_predictive_sim_policy_plan.py`
- `apply_predictive_sim_policy_plan.py`
- `run_predictive_control_plane.py`
- `predictive_control_snapshot` 控制动作
- 前端“预测控制面”状态面板

已完成的 300 节点验证：

- 停掉原有 DV agent 与 DV 控制进程
- 生成 `run/predictive_route_plan_300.json`
- 生成 `run/predictive_sim_policy_plan_300.json`
- 将预测路由 slot 实际下发到 300 个容器
- 将预测 sim policy slot 实际写入 `star300lite_sim`

当前结果：

- `300` 个容器路由应用成功
- 当前选中 slot 的主连通分量规模为 `268`
- 其余 `32` 个节点在该 slot 中为孤立点
- 因此当前验证结果是“300 节点完成预测控制面下发”，不是“300 节点全连通”
- 预测控制器已实测从 `slot 2` 自动切换到 `slot 3`
- 前端可读取当前 `route slot / sim slot / apply 状态 / rule_count`
