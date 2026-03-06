# Issue: 动态拓扑下发自动化（容器静态路由 + erv300_sim 策略）

## 背景

当前 300 节点实验环境同时存在两类“拓扑落地”需求：

1. 将实时拓扑转换为每个节点容器内的静态路由（`ip route`）。
2. 将实时拓扑转换为环境仿真器 `erv300_sim` 的二层转发策略（`/opt/sim/policy.json`）。

手工更新成本高，且容器名称/ID变化、代理环境变量、网络模式（`none`）等因素容易导致执行失败。

## 目标

构建可复用的自动化脚本，基于同一份实时拓扑流稳定、可回滚地完成两种下发。

## 交付范围

1. `scripts/push_static_routes.py`
   - 订阅 WS 拓扑，按稳定帧计算最短路下一跳并增量下发静态路由。
   - 支持 `--dry-run` / `--once` / `--min-stable-frames`。
   - 支持 `container_name` + `container_id` 回退解析。
   - 在 `docker inspect` 无 IP 时，回退到容器内 `ip addr` 提取。

2. `scripts/push_sim_policy.py`
   - 订阅 WS 拓扑，将 `links` 转换为 `erv300_sim` 双向 `unicast/forward` 规则。
   - 通过 `veth_0` MAC 建立节点映射并写入 `policy.json`。
   - 写入后自动向 `l2_center_sim.py` 发送 `HUP` 热重载。
   - 支持 `--dry-run` / `--once` / `--min-stable-frames`。

3. 文档与数据
   - README 增补两类脚本使用方法。
   - `docs/node_mapping_300.csv` 对齐当前 `erv300_r_*` 容器命名。

## 验收标准

1. 在 `--dry-run --once` 模式下，脚本可成功消费稳定帧并输出规则/路由摘要。
2. 在真实下发模式下：
   - 静态路由脚本可无批量解析错误执行；
   - 仿真器策略脚本可触发 `l2_center_sim.py` 重载（`reload` 计数增加）。
3. 对于代理环境，脚本能避免本地 WS 被 `http_proxy/https_proxy` 干扰。

## 风险与注意事项

1. 当前拓扑可能非全连通，属于模型输出，不应误判为下发失败。
2. 回归测试中应先备份 `policy.json`，测试结束后恢复。
3. 广播/组播验证需要配合抓包口径，避免将空行误判为“抓到报文”。
