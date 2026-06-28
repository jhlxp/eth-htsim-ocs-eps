# Huawei 8192 卡集群拓扑与 M/N 公式

这份文档记录 `需求/组网方案-专家数据-问题约束_v3.pdf` 的修订口径、8192 卡集群的 `M/N` 参数族，以及 OCS 是否全连接的判断。当前功能测试口径使用 `N=8, M=4`，它仍然是 8192 卡集群，只是每张 XPU 有 4 个外部并行端口/plane。当前结论是：PDF 拓扑整体没有大问题，只需要把第 3 页的 `L1 电交换层共 1024 * M / N 台 EPS` 修订为 `512 * M / N 台 EPS`。

## 8192 卡规模不随 M/N 变化

PDF 里的 `M` 和 `N` 调的是网络组织方式，不改变总卡数：

```text
每个 L0 domain = 8 tray * 8 XPU = 64 卡
每个 group 有 N 个 L0 domain
group 数 = 128 / N

总卡数
  = (128 / N) * N * 64
  = 8192
```

因此刚才遍历的 `N=1,2,4,8,16,32,64`，无论 `M=1/2/4`，都是同一个 8192 卡集群；区别在于每个 group 多大、L1 EPS 数量、OCS 数量、OCS 互联是否足够全连接。

| N | group数 128/N | 每group卡数 64N | 总卡数 |
|---:|---:|---:|---:|
| 1 | 128 | 64 | 8192 |
| 2 | 64 | 128 | 8192 |
| 4 | 32 | 256 | 8192 |
| 8 | 16 | 512 | 8192 |
| 16 | 8 | 1024 | 8192 |
| 32 | 4 | 2048 | 8192 |
| 64 | 2 | 4096 | 8192 |

`M` 表示每张 XPU 的外部并行端口/plane 数，也会线性改变 L1 EPS 和 OCS 端口规模；但它不进入总卡数公式。

## PDF 修订点

L0/L1 页可以直接推出：

```text
groups = 128 / N
每 group 每 L1 plane 有 4 台 L1 EPS
L1 plane 数量 = M

L1 EPS 总数
  = groups * M * 4
  = (128 / N) * M * 4
  = 512M / N
```

因此 PDF 第 3 页的：

```text
L1 电交换层共 1024 * M / N 台 EPS
```

应修订为：

```text
L1 电交换层共 512 * M / N 台 EPS
```

其他 L2 公式保持：

```text
OCS 台数 = 8N
每台 OCS 端口 = 512M/N
每台 L1 EPS 上行端口 = 8N
```

端口闭合：

```text
L1 总上行端口 = (512M/N) * (8N) = 4096M
OCS 总端口 = (8N) * (512M/N) = 4096M
```

## L0/L1 拓扑口径

一个 L0 互联域：

```text
8 个 compute tray
每个 compute tray 有 8 张 XPU
每个 tray 内 8 卡 full-mesh
每张 XPU 有 M 个外部端口连接 M 个 L0 plane
```

每个 L0 plane：

```text
8 台 L0 EPS
每台 L0 EPS 下行 8 端口
每台 L0 EPS 上行 4 端口
L0 收敛比 = 8:4 = 1:2
```

每个 group/L1 互联域：

```text
N 个 L0 互联域
M 个 L1 plane
每个 L1 plane 有 4 台 L1 EPS
每台 L1 EPS 下行 8N 端口，上行 8N 端口
L1 收敛比 = 1:1
```

## OCS 合口与逻辑节点

合口规则：

```text
同一个 L1 plane 内：
  EPS1/EPS2 同号口合口
  EPS3/EPS4 同号口合口

0-index:
  EPS0/EPS1 => coupled_pair 0
  EPS2/EPS3 => coupled_pair 1
```

物理 L1 EPS 总数：

```text
E = 512M/N
```

合口后的逻辑 OCS 节点数：

```text
Q = E / 2 = 256M/N
```

每 group 的逻辑 OCS 节点数：

```text
S = Q / groups = (256M/N) / (128/N) = 2M
```

逻辑节点建议编号：

```text
logical_node = (group, l1_plane, coupled_pair)

l1_plane in [0, M)
coupled_pair in [0, 2)
```

物理 L1 EPS 到逻辑节点：

```text
coupled_pair = l1_eps_index / 2
coupled_member = l1_eps_index % 2
```

## OCS 是否全连接

设：

```text
Q = 256M/N        # 合口后逻辑节点数
K = 8N            # 物理 OCS 台数
S = 2M            # 每 group 逻辑节点数
```

如果要求所有逻辑节点任意两点都有直连：

```text
K >= Q - 1
```

代入：

```text
8N >= 256M/N - 1
```

等价于：

```text
N^2 + N/8 >= 32M
```

近似判断：

```text
N^2 >= 32M
```

如果按 PDF 语义，同 group 内不走 OCS，只要求跨 group 全连接：

```text
K >= Q - S
8N >= 256M/N - 2M
```

默认仿真采用跨组口径。

## OCS 个数取 min

物理 OCS 数量：

```text
OcsSwitchesPhysical = 8N
```

用于生成唯一跨组全连接边的有效轮数：

```text
OcsEffectiveDegree = min(8N, 256M/N - 2M)
```

如果需要包含同 group 完整全连接：

```text
OcsEffectiveFullDegree = min(8N, 256M/N - 1)
```

默认按 `OcsEffectiveDegree` 和跨组最大度自动决定：

```text
if OcsEffectiveDegree == 256M/N - 2M:
  OcsTopology complete_cross_group
else:
  OcsTopology sparse_expander

OcsEffectiveDegree = min(8N, 256M/N - 2M)
```

当 `8N` 大于 `OcsEffectiveDegree` 时，多余 OCS 轮次可以记为：

```text
redundant_ocs_rounds = 8N - OcsEffectiveDegree
```

它们可以后续用于重复边、额外带宽或重构预留；第一版仿真先不强制使用。

## M=4 时的 N 扫描

`M=4`：

```text
E = 2048/N
Q = 1024/N
K = 8N
S = 8
P = 2048/N        # 每台 OCS 端口
U = 8N            # 每 L1 EPS 上行端口
```

| N | group数 128/N | L1 EPS数 E | 合口后Q | OCS数K | 每OCS端口P | 每L1上行U | L1总上行端口 | OCS总端口 | 端口够? | 全连接需 Q-1 | 全连接? | 跨组全连接需 Q-8 | 跨组全连接? |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|---:|:---:|---:|:---:|
| 1 | 128 | 2048 | 1024 | 8 | 2048 | 8 | 16384 | 16384 | 是 | 1023 | 否 | 1016 | 否 |
| 2 | 64 | 1024 | 512 | 16 | 1024 | 16 | 16384 | 16384 | 是 | 511 | 否 | 504 | 否 |
| 4 | 32 | 512 | 256 | 32 | 512 | 32 | 16384 | 16384 | 是 | 255 | 否 | 248 | 否 |
| 8 | 16 | 256 | 128 | 64 | 256 | 64 | 16384 | 16384 | 是 | 127 | 否 | 120 | 否 |
| 16 | 8 | 128 | 64 | 128 | 128 | 128 | 16384 | 16384 | 是 | 63 | 是 | 56 | 是 |
| 32 | 4 | 64 | 32 | 256 | 64 | 256 | 16384 | 16384 | 是 | 31 | 是 | 24 | 是 |
| 64 | 2 | 32 | 16 | 512 | 32 | 512 | 16384 | 16384 | 是 | 15 | 是 | 8 | 是 |

## M=1/2/4 的全连接富余比例

比例：

```text
K / (Q - 1)
  = 8N / (256M/N - 1)
  = 8N^2 / (256M - N)
```

大于等于 1 表示完整全连接 OCS 轮数足够。

| M | N | 合口后Q | OCS数K | K/(Q-1) | 全连接? |
|---:|---:|---:|---:|---:|:---:|
| 1 | 1 | 256 | 8 | 0.031 | 否 |
| 1 | 2 | 128 | 16 | 0.126 | 否 |
| 1 | 4 | 64 | 32 | 0.508 | 否 |
| 1 | 8 | 32 | 64 | 2.065 | 是 |
| 1 | 16 | 16 | 128 | 8.533 | 是 |
| 1 | 32 | 8 | 256 | 36.571 | 是 |
| 1 | 64 | 4 | 512 | 170.667 | 是 |
| 2 | 1 | 512 | 8 | 0.016 | 否 |
| 2 | 2 | 256 | 16 | 0.063 | 否 |
| 2 | 4 | 128 | 32 | 0.252 | 否 |
| 2 | 8 | 64 | 64 | 1.016 | 是 |
| 2 | 16 | 32 | 128 | 4.129 | 是 |
| 2 | 32 | 16 | 256 | 17.067 | 是 |
| 2 | 64 | 8 | 512 | 73.143 | 是 |
| 4 | 1 | 1024 | 8 | 0.008 | 否 |
| 4 | 2 | 512 | 16 | 0.031 | 否 |
| 4 | 4 | 256 | 32 | 0.125 | 否 |
| 4 | 8 | 128 | 64 | 0.504 | 否 |
| 4 | 16 | 64 | 128 | 2.032 | 是 |
| 4 | 32 | 32 | 256 | 8.258 | 是 |
| 4 | 64 | 16 | 512 | 34.133 | 是 |

## N=8, M=4 测试点

```text
N = 8
M = 4
groups = 16

L1 EPS total = 256
logical OCS nodes Q = 128
logical nodes per group S = 8

OCS physical switches K = 64
OCS ports per switch = 256
L1 uplinks per EPS = 64

OcsEffectiveCrossDegree = min(64, 128 - 8) = 64
OcsEffectiveFullDegree = min(64, 128 - 1) = 64
redundant rounds under cross-group mode = 0
```

默认仿真建议：

```text
OcsTopology sparse_expander
OcsEffectiveDegree 64
OcsSwitchesPhysical 64
OcsNoIntraGroup true
```

## HTSIM 配置建议

```text
Topology Huawei
L2Mode ocs

N 8
M 4
Groups 16

L0DomainsPerGroup 8
L0Planes 4
L0EpsPerPlanePerL0Domain 8
L0DownlinksPerEps 8
L0UplinksPerEps 4

L1Planes 4
L1EpsPerPlane 4
L1EpsTotal 256

OcsSwitchesPhysical 64
OcsPortsPerSwitch 256
OcsLogicalNodes 128
OcsLogicalNodesPerGroup 8
OcsTopology sparse_expander
OcsEffectiveDegree 64
OcsNoIntraGroup true
OcsBidirectional true
OcsExpanderSeed 42
```

链路速率建议先按功能验证口径：

```text
LinkSpeedRankLocalGbps 800
LinkSpeedRankL0Gbps 100
LinkSpeedL0L1Gbps 100
LinkSpeedL1OcsGbps 100
```

路由可调参数：

```text
OcsRouting direct | ecmp | spraypoint | ksp

SprayPointP min(4, OcsEffectiveDegree)
SprayPointH min(2, OcsEffectiveDegree)
SprayPointLevels auto
SprayPointSeed 42
SprayPointChoice packet_rr | flow_hash

OcsKspK 8
OcsKspMaxHops auto
OcsKspSeed 42
OcsKspChoice packet_rr | packet_hash | flow_hash
```

## 当前代码状态

当前代码已有这些模块：

```text
huawei_ocs_graph
huawei_ocs_spraypoint
huawei_ocs_ksp
dump/smoke 工具
```

当前已对齐本文档口径：

```text
1. `--coupled` OCS graph node 是 logical_node(group, l1_plane, coupled_pair)，N=8,M=4 时是 128 个节点。
2. degree 等于跨组最大度时，构造 complete_cross_group 图。
3. degree 小于跨组最大度时，构造 sparse_expander/random graph。
4. N=8,M=4 测试点应使用 `OcsEffectiveDegree=min(64,128-8)=64`；它等于物理 OCS 数 64，但仍然小于跨组全连接所需的 120。
5. SprayPoint/KSP 运行在 logical OCS 图上。
6. dump 输出会给出 logical node 和物理 L1 EPS 合口成员映射。
7. `edge_ocs` 会给出 OCS matching round id；每个 round 是覆盖全部 logical node 的 matching。
```

## 推荐验证顺序

1. 构造逻辑 OCS 图：

```text
N=8
M=4
groups=16
logical_nodes=128
logical_nodes_per_group=8
effective_degree=64
topology=sparse_expander
```

检查：

```text
无 intra-group edge
每个 logical node 跨组邻居数 = 64
不是跨组任意 pair 都有直接 edge
物理 OCS 数量仍记录为 64
```

2. 对同一张图分别 dump：

```text
direct/ECMP next hops
SprayPoint parents/source-neighbors
KSP paths
```

3. 单 flow 功能验证：

```text
same tray: direct 800G local link
same group different tray: L0/L1 group-local path
cross group L2-OCS direct/ECMP path
cross group L2-OCS SprayPoint path
cross group L2-OCS KSP path
```

4. 小 traffic matrix 验证 link load：

```text
L1-OCS links 100G
rank-local links 800G
1ms link_load sampler
```

5. 再接 DeepSeek-R1 EP256 workload。
