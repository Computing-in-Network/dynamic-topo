# 300节点动态拓扑计算规格说明书 (V1.0)

## 1. 节点规模与属性
- **总节点数 (N)**: 300
- **LEO-Polar (L1)**: 100个。轨道高度 550km，倾角 97.6°（太阳同步）。
- **LEO-Inclined (L2)**: 100个。轨道高度 550km，倾角 53°。
- **Aircraft (A1)**: 50个。高度 10km，速度 250m/s，随机航线。
- **Ship (S1)**: 50个。高度 0km，速度 10m/s，随机航线。

## 2. 数学模型约束
- **时间系统**: 采用 UTC，步长 $\Delta t = 1.0s$。
- **坐标系统**: 
  - 卫星传播：ECI (TEME)
  - 拓扑计算：ECEF (WGS84)
  - 节点输入：LLA (Lat, Lon, Alt)
- **距离度量**: 3D 欧几里得距离 $d = \| \mathbf{P}_{1} - \mathbf{P}_{2} \|_2$。
- **遮挡检查**: 
  - 视线 (LoS) 判定：连线不穿过地心半径 $R_e = 6371km$ 的球体。
  - 算法：$\min \| \mathbf{P}_1 + t(\mathbf{P}_2 - \mathbf{P}_1) \| > R_e, t \in [0, 1]$。

## 3. 技术协议 (Implementation)
- **计算库**: `skyfield` (SGP4), `numpy` (Vectorization).
- **性能**: 300x300 矩阵计算 + Redis 写入需在 100ms 内完成。
- **存储 (Redis)**:
  - `node:pos`: Hash 类型，存储最新 ECEF 坐标。
  - `topo:adjacency`: Stream 类型，存储连通性位图 (Bitmap)。

## 4. 验收准则 (DoD)
- [ ] 300 节点位置更新频率恒定 1Hz。
- [ ] 拓扑矩阵对称性校验通过 ($A = A^T$)。
- [ ] Docker 容器内内存占用 < 512MB。