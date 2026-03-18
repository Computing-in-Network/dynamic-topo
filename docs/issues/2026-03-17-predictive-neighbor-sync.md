# 星历预测邻居同步

## 背景

当前仓库已经具备：

- `build_predictive_route_plan.py` / `apply_predictive_route_plan.py`
- `build_predictive_sim_policy_plan.py` / `apply_predictive_sim_policy_plan.py`
- `run_predictive_control_plane.py`

也就是说，预测控制面已经可以按时间窗口切换：

- 300 个节点的预测静态路由
- `star300lite_sim` 的预测二层策略

但当前多跳端到端仍未打通。已有排查表明：

1. 星历预测路径本身已经算出来
2. 路由表已按 slot 下发
3. `sim policy` 已按 slot 下发
4. 中间跳仍会因邻居解析缺失而断流

根因是当前预测控制面缺了一层关键状态：

- 直连邻居 `ip -> mac` / `ip neigh` 同步

在现有模型下，`sim policy` 默认只放行 `unicast`，不放开 ARP 广播。因此如果某一跳没有永久邻居项，即使路由和策略都正确，数据面仍无法完成逐跳转发。

## 目标

1. 为预测控制面补齐“邻居同步”这一层
2. 生成未来时间窗口的预测邻居计划
3. 支持按 slot 将当前直连邻居的永久邻居表同步到容器
4. 将邻居计划接入 `run_predictive_control_plane.py`，与 route / sim policy 一起定时切换
5. 验证多跳端到端转发相较当前版本得到改善

## 范围

1. 新增预测邻居计划生成脚本
   - 基于未来拓扑窗口
   - 提取每个 slot 的直连邻居集合
   - 记录邻居 IP / MAC / 容器映射

2. 新增预测邻居计划应用脚本
   - 对每个节点下发当前 slot 的永久邻居项
   - 清理不再属于当前 slot 的永久邻居项

3. 扩展预测控制器
   - 在 route slot / sim slot 切换时同步 neighbor slot
   - 增加 neighbor state 输出

4. 文档补充
   - 如何生成 / 应用 neighbor plan
   - 如何与 predictive control plane 一起运行
   - 当前边界与验证结果

## 验收标准

1. 能生成未来时间窗口的 `predictive_neighbor_plan.json`
2. 能按 slot 对 300 个节点执行邻居同步
3. `run_predictive_control_plane.py` 能联动切换：
   - route slot
   - sim policy slot
   - neighbor slot
4. 至少验证一条当前已知多跳预测路径的数据面改善情况
5. 文档清楚说明该层的作用与边界

## 当前进展

已完成：

1. 新增预测邻居计划生成脚本 `build_predictive_neighbor_plan.py`
2. 新增预测邻居计划应用脚本 `apply_predictive_neighbor_plan.py`
3. 扩展 `run_predictive_control_plane.py`
   - 支持 `--neighbor-plan`
   - 输出 `neighbor_state.json`
   - 将 `neighbor_slot_index` / `neighbor_ok` 写入控制器状态
4. 扩展 `stream_server.py`
   - `predictive_control_snapshot` 现会返回 `neighbor_state`
5. 扩展前端“预测控制面”面板
   - 展示 `neighbor slot`
   - 展示邻居 apply 状态
   - 展示 `neighbor_upserts / neighbor_deletes`

## 已验证结果

1. 已生成 `run/predictive_neighbor_plan_300.json`
   - `nodes=300`
   - `samples=11`
   - `slots=11`
2. 已对 `slot 9` 做真实邻居同步下发
   - `apply_ok=300`
   - `apply_fail=0`
   - `neighbor_upserts=948`
3. 已对 `slot 0` 做 route + neighbor + sim 的一次联调
   - `route_slot=0 apply_ok=300`
   - `neighbor_slot=0 apply_ok=300`
   - `sim_slot=0 policy_apply=ok`
4. 已复测已知多跳路径：
   - `star300lite_r_1 -> 10.255.0.100`
   - 使用 `ping -I 10.255.0.1 -c 1 -W 2 10.255.0.100`
   - 实测成功

## 当前边界

1. 邻居同步解决的是“下一跳 MAC 解析缺失”
2. 普通 `ping 目标loopback` 默认源地址仍可能选成 `10.20.x.x`
   - 当前验证建议显式使用 `-I <源节点 loopback>`
3. 若未来拓扑本身裂开为多个连通分量，neighbor sync 不会虚构不可达链路

## 注意事项

1. 邻居同步解决的是“下一跳 MAC 解析缺失”，不是把本来断开的拓扑强行变成全连通
2. 若未来拓扑本身裂成多个连通分量，预测邻居同步也只能如实反映不可达
3. 应避免误删容器内与当前实验无关的邻居项；脚本需限定管理范围
4. 需明确与旧的 DV 邻居同步脚本职责边界，避免混用
