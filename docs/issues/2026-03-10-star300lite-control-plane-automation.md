# Issue: star300lite 动态拓扑控制面自动化

## 背景

当前 300 节点环境已从 erv300 系列切换为 star300lite 系列：

- 节点容器为 star300lite_r_1 到 star300lite_r_300
- 仿真器容器为 star300lite_sim
- 节点仍使用 veth_0 作为业务口，lo 上挂载节点 /32
- star300lite_sim 依赖 sim_in 和 sim_out 与宿主机 OVS 口 s3l_sinh 和 s3l_south 对接

现阶段虽然 dynamic-topo 已能计算实时拓扑，但从“拓扑结果”到“仿真器策略 + 300 节点静态路由”的落地流程仍缺少成体系自动化，重建容器后恢复成本高，且持续更新容易受到代理环境、容器名变化和高频重灌的影响。

## 目标

构建适配 star300lite 环境的控制面自动化流程，使实时拓扑可以稳定地下发到：

1. star300lite_sim 的 policy.json
2. 300 个节点容器内的 Linux 静态路由表

并支持容器重建后的快速恢复与后台常驻运行。

## 交付范围

1. 映射生成
   - 新增按容器前缀生成 node_mapping_300.csv 的脚本
   - 支持 star300lite_r_ 命名体系

2. 仿真器恢复
   - 新增脚本恢复 sim_in 和 sim_out
   - 校验 OVS 中 s3l_sinh 和 s3l_south 的绑定关系
   - 拉起 l2_center_sim.py

3. 仿真器策略下发
   - push_sim_policy.py 支持自动推断对应的 sim 容器
   - 兼容本地 WS 代理干扰
   - 支持最小应用间隔，避免高频重载

4. 节点静态路由下发
   - push_static_routes.py 兼容本地 WS 代理干扰
   - 支持最小应用间隔，避免高频重灌
   - 维持增量路由下发策略

5. 常驻运行
   - 新增启动和停止脚本
   - 自动完成“生成映射 -> 恢复仿真器 -> 后台拉起策略控制器与路由控制器”

6. 文档
   - README 与映射说明文档同步到 star300lite 方案

## 验收标准

1. start_star300lite_control_plane.sh 可成功完成：
   - 映射文件重建
   - star300lite_sim 接口恢复
   - l2_center_sim.py 启动
   - 两个后台控制器常驻

2. push_sim_policy.py 能在 star300lite_sim 中成功写入策略并触发 reload

3. push_static_routes.py 能在 300 个节点上完成一次全量初始路由下发，且后续按增量更新

4. 控制器可通过 min-apply-interval-s 控制更新频率，默认建议起步值为 30 秒

## 风险与注意事项

1. docs/node_mapping_300.csv 含容器 ID，随环境重建可能变化，应以脚本重建为准
2. run 目录为运行时日志目录，不应纳入版本控制
3. rootbak 文件仅是本地编辑备份，不应纳入版本控制
