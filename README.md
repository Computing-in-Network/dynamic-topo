# dynamic-topo

300 节点动态拓扑计算与 Redis 写入示例实现。

## 功能

- 300 节点构成：L1(100) + L2(100) + A1(50) + S1(50)
- 1Hz 时间步推进（默认 `dt=1.0s`）
- ECI 到 ECEF 的卫星位置传播，LLA 到 ECEF 的移动平台转换
- 300x300 LoS 拓扑矩阵计算与对称性保证
- Redis 写入：
  - `node:pos` (Hash)
  - `topo:adjacency` (Stream, bitmap hex)
- 无 Redis 服务时自动回退到内存后端，方便开发测试

## 运行

```bash
pip install -r requirements.txt
python main.py --steps 5 --dt 1.0
```

## 测试

```bash
pytest
```
