# node_mapping_300 说明

文件: `docs/node_mapping_300.csv`

## 字段定义

- `node_id`: 拓扑节点 ID
- `node_index`: 拓扑序号 (1..300)
- `container_name`: 对应容器名（示例：`star300lite_r_<index>`）
- `container_id`: Docker 容器 ID（短 ID）
- `container_status`: `docker inspect` 返回的容器状态（如 `running`、`exited`）

## 映射规则

- `1..100` -> `SAT-POLAR-001..SAT-POLAR-100`
- `101..200` -> `SAT-INCL-001..SAT-INCL-100`
- `201..250` -> `AIR-001..AIR-050`
- `251..300` -> `SHIP-001..SHIP-050`

## 生成方式

```bash
python3 scripts/generate_node_mapping.py \
  --container-prefix star300lite_r_ \
  --output docs/node_mapping_300.csv
```

- 仅使用 `docker inspect` 只读采集容器元数据
- 不执行 `stop/restart/exec`，不会影响容器运行
