# Huawei L2-OCS 拓扑设计

这份文档描述 `L2Mode=ocs` 的拓扑口径。当前结论是：`需求/组网方案-专家数据-问题约束_v3.pdf` 的 L0/L1/L2 设计整体是自洽的，只需要把第 3 页中 `L1 电交换层共 1024 * M / N 台 EPS` 修订为 `512 * M / N 台 EPS`。其余 OCS 数量、OCS 端口数、L1 上行端口数仍按 PDF 原公式理解。

## 修订后的 PDF 公式

通用参数：

```text
N = 每个 group 内的 L0 互联域数量
M = L0/L1 电交换平面数量
```

L0/L1 推导：

```text
groups = 128 / N
每个 group 每个 L1 plane 有 4 台 L1 EPS
l1_eps_total = groups * M * 4
              = (128 / N) * M * 4
              = 512 * M / N
```

L2 OCS 口径：

```text
ocs_switches_physical = 8 * N
ocs_ports_per_switch = 512 * M / N
l1_uplinks_per_eps = 8 * N
```

端口闭合：

```text
L1 总上行端口 = l1_eps_total * l1_uplinks_per_eps
              = (512M/N) * (8N)
              = 4096M

OCS 总端口 = ocs_switches_physical * ocs_ports_per_switch
           = (8N) * (512M/N)
           = 4096M
```

所以只修订 L1 EPS 数量后，L1 上行端口总数和 OCS 端口总数仍然刚好相等。

## 合口后的逻辑 OCS 节点

PDF 第 3 页说：

```text
L1 每两个 EPS 同号口合并后连接至 OCS
EPS1 与 EPS2 同号口合并
EPS3 与 EPS4 同号口合并
```

因此 OCS 路由层不应直接把每台物理 L1 EPS 当成独立 OCS 图节点，而应先做合口：

```text
logical_ocs_nodes = l1_eps_total / 2
                  = 256 * M / N
```

每个 group 的逻辑 OCS 节点数：

```text
logical_ocs_nodes_per_group
  = logical_ocs_nodes / groups
  = (256M/N) / (128/N)
  = 2M
```

建议逻辑节点编号：

```text
logical_node = (group, l1_plane, coupled_pair)

l1_plane in [0, M)
coupled_pair in [0, 2)

0-index:
  coupled_pair 0 表示 EPS0/EPS1 同号口合口
  coupled_pair 1 表示 EPS2/EPS3 同号口合口
```

物理 L1 EPS 仍然存在，编号为：

```text
physical_l1 = (group, l1_plane, l1_eps_index)
l1_eps_index in [0, 4)
```

映射关系：

```text
coupled_pair = l1_eps_index / 2
coupled_member = l1_eps_index % 2
```

路由算法工作在 `logical_node` 图上；落到物理链路统计时，再映射到对应的 OCS 端口/合口。

## OCS 是否全连接

设：

```text
Q = logical_ocs_nodes = 256M / N
K = ocs_switches_physical = 8N
S = logical_ocs_nodes_per_group = 2M
```

一台 OCS 可以抽象为一轮 matching。若要让 `Q` 个逻辑节点任意两点都有一条直连，需要：

```text
K >= Q - 1
```

代入 `M,N`：

```text
8N >= 256M/N - 1
```

等价于：

```text
N^2 + N/8 >= 32M
```

常用近似判断：

```text
N^2 >= 32M
```

如果按 PDF 设计“不在同 group 内通过 OCS 互联”，只要求跨 group 全连接，则每个逻辑节点不需要连接本 group 的 `S=2M` 个逻辑节点，所需 OCS 轮数是：

```text
D_cross = Q - S = 256M/N - 2M
```

跨组全连接条件：

```text
K >= D_cross
8N >= 256M/N - 2M
```

## OCS 数量取值

物理 OCS 数量仍然是 PDF 公式：

```text
OcsSwitchesPhysical = 8N
```

用于逻辑 OCS 图构造的实际 degree 默认取：

```text
OcsEffectiveDegree = min(8N, 256M/N - 2M)
```

如果需要包含同 group 逻辑节点的完整全连接，则取：

```text
OcsUniqueFullConnect = min(8N, 256M/N - 1)
```

当前问题语义中，同 group 内流量走 L0/L1 组内网络，因此默认使用跨组口径：

```text
if OcsEffectiveDegree == 256M/N - 2M:
  OcsTopology complete_cross_group
else:
  OcsTopology sparse_expander
```

如果 `8N` 大于跨组全连接所需轮数，剩余 OCS 可以视为：

```text
redundant OCS rounds
重复边 / 额外带宽 / 预留重构资源
```

第一版仿真生成 degree 为 `OcsEffectiveDegree` 的逻辑图，保留物理 `8N` 作为配置和日志字段。对于 `N=8,M=4`，`OcsEffectiveDegree=64 < 120`，因此应生成 sparse expander。

## N=8, M=4 当前功能测试口径

代入：

```text
groups = 128 / 8 = 16
l1_eps_total = 512 * 4 / 8 = 256
Q = 256 / 2 = 128
S = 2M = 8
K = 8N = 64
```

完整全连接需要：

```text
Q - 1 = 127
```

跨组全连接需要：

```text
Q - S = 128 - 8 = 120
```

因此：

```text
64 台 OCS 不足以覆盖完整全连接，也不足以覆盖跨组全连接。
当前功能测试应使用 sparse_expander，degree = 64。
```

端口：

```text
每台 OCS 端口 = 512 * 4 / 8 = 256
每台 L1 EPS 上行端口 = 8 * 8 = 64
L1 总上行端口 = 256 * 64 = 16384
OCS 总端口 = 64 * 256 = 16384
```

## 拓扑生成策略

第一版建议支持两种 OCS topology mode：

```text
OcsTopology complete_cross_group
OcsTopology sparse_expander
```

默认按 `OcsEffectiveDegree` 与跨组最大度自动决定：

```text
if OcsEffectiveDegree == Q - 2M:
  OcsTopology complete_cross_group
else:
  OcsTopology sparse_expander
```

`complete_cross_group`：

```text
节点是 logical_node = (group, l1_plane, coupled_pair)
禁止同 group 边
用多轮 matching 覆盖所有跨 group pair
轮数 = min(8N, Q - 2M)
```

`sparse_expander`：

```text
用于 K < Q - 2M 的配置，或者主动研究稀疏 OCS 图
节点仍是 logical_node
degree = min(8N, configured_degree)
禁止同 group 边
随机种子 OcsExpanderSeed 控制可复现
```

不要再把 OCS 拓扑理解成“每个 L1 plane 一份随机图，然后复制到其他 plane”。`M` 已经体现在逻辑节点集合里；OCS 是覆盖所有 group、所有 L1 plane、所有合口 pair 的一个整体 L2 合平面。

## 路由语义

OCS 层只负责跨 group：

```text
same group:
  不走 OCS，走 L1/L0 group-local path

cross group:
  从源 group 的 logical_node 进入 OCS 图
  到达 dst_group 的任意 logical_node 后，OCS 段结束
  再进入目的 group 内部下行
```

如果 `OcsTopology=complete_cross_group`，跨组两点之间存在直接逻辑 circuit，SprayPoint/KSP 会退化为一跳或近似一跳策略。

如果 `OcsTopology=sparse_expander`，SprayPoint/KSP 才真正承担多跳跨组路由。

## HTSIM 配置字段建议

```text
Topology Huawei
L2Mode ocs

N 8
M 4
Groups 16

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

链路速率建议：

```text
LinkSpeedRankLocalGbps 800
LinkSpeedRankL0Gbps 100
LinkSpeedL0L1Gbps 100
LinkSpeedL1OcsGbps 100
```

## 当前代码状态

当前 `huawei_ocs_graph`、SprayPoint、KSP dump 工具已经按本口径修订：

```text
1. `--coupled` OCS 图节点是 logical_node = (group, l1_plane, coupled_pair)。
2. degree 等于跨组最大度时，构造 complete_cross_group 图。
3. degree 小于跨组最大度时，构造 sparse expander / circulant fallback。
4. N=8,M=4 测试点使用 OcsEffectiveDegree = min(64, 128 - 8) = 64，因此是 sparse_expander，不是 complete_cross_group。
5. SprayPoint/KSP 都运行在 logical OCS 图上。
6. dump 输出会标明 logical node 到物理 L1 EPS 合口成员的映射。
7. `edge_ocs` 记录每条逻辑边属于哪一轮 OCS matching；每一轮覆盖所有 logical node 一次。
```

功能测试脚本：

```text
tests/functional/run_ocs_m4n8_feature_tests.sh
tests/functional/run_huawei_ocs_dataplane_tests.sh
```
