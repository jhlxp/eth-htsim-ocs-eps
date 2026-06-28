# Huawei L2-OCS SprayPoint 设计

这份文档只描述 `L2Mode=ocs` 下的 SprayPoint 路由。基础 OCS 拓扑、L1 EPS 修订公式、合口逻辑节点和 OCS 是否全连接的判断见 `02_l2_ocs_design.md`。

## 目标语义

SprayPoint 是 L2-OCS 专用的跨组 next-hop policy：

```text
src logical OCS node
  -> source-only spray
  -> destination-specific pointing
  -> any logical OCS node in dst_group
  -> group-local downlink
```

OCS 层的目标是 `dst_group`，不是具体 `dst_l1_eps`。一旦到达目的 group 的任意逻辑 OCS 节点，OCS 段结束，后续由目的 group 内 L1/L0/tray/rank 下行。

## 图粒度

SprayPoint 运行在合口后的逻辑 OCS 图上：

```text
node = logical_node(group, l1_plane, coupled_pair)
edge = cross-group OCS circuit between two logical nodes
```

其中：

```text
logical_nodes = 256M/N
logical_nodes_per_group = 2M
```

不要再把 SprayPoint 状态建立在 `(group, eps_index)` 后复制到每个 plane。`M` 已经进入逻辑节点集合，OCS 是一个整体 L2 合平面。

## 目的地集合

目的地定义为 destination group 的所有逻辑 OCS 节点：

```text
dst_set(group g) = all logical_node(g, *, *)
```

状态量是：

```text
groups 个 destination states
每个 state 覆盖 logical_nodes 个节点
```

这避免了 `rank_count^2` 的端到端路径表。

## 和全连接 OCS 的关系

如果 `OcsTopology=complete_cross_group`，任意两个跨 group 逻辑节点之间都有直接 circuit：

```text
src_group != dst_group => one-hop OCS circuit exists
```

此时 SprayPoint 基本退化为一跳策略：

```text
source_spray_next_hops(src) 中包含 dst_group 的直接邻居
pointing 阶段通常可以直接指向 dst_set
```

因此 SprayPoint 的主要价值在：

```text
OcsTopology=sparse_expander
或者 OCS 数量不足以覆盖完整跨组全连接
或者主动保留稀疏/重构后的 OCS 图做多跳路由研究
```

## Forwarding State

对每个 `dst_group` 构建一次状态：

```text
D = dst_group 内所有 logical OCS nodes
wp0 = neighbors(D) - D
wp_level[i+1] = 从 wp_level[i] 的邻居中按 spray_p 选择未分配节点
inner_ring = wp_last 的未分配邻居
outer_ring = 其他节点
```

每个非目的节点保存 parent candidates：

```text
node in wp0:
    parents(node) = neighbors(node) ∩ D，最多 SprayPointH 个

node in wp_level[k]:
    parents(node) = neighbors(node) ∩ wp_level[k-1]，最多 SprayPointH 个

node in inner_ring:
    parents(node) = neighbors(node) ∩ wp_last，最多 SprayPointH 个

node in outer_ring:
    parents(node) = shortest next hops toward inner_ring，最多 SprayPointH 个

fallback:
    shortest next hops toward D，最多 SprayPointH 个
```

## Next-Hop 选择

L1 EPS 收到包后：

1. 如果目的 rank 在本 group：

```text
不走 OCS，走组内 L1 -> L0 -> rank
```

2. 如果目的 rank 在其他 group，且包从 L0/rank 侧进入 L1：

```text
current_logical_node = physical L1 EPS 映射到的合口 logical_node
source-only spray:
    candidates = OCS neighbors(current_logical_node)
```

3. 如果包从 OCS 侧进入 L1，且当前 logical node 不属于目的 group：

```text
pointing:
    candidates = parents[dst_group][current_logical_node]
```

4. 如果包从 OCS 侧进入目的 group：

```text
停止 OCS 转发
转入组内下行：
    L1(dst_group, *, *) -> L0(dst) -> rank(dst)
```

## 参数

建议参数：

```text
OcsRouting spraypoint
SprayPointP 4
SprayPointH 2
SprayPointLevels auto
SprayPointSeed 42
SprayPointChoice packet_rr | flow_hash
```

含义：

- `SprayPointP`：waypoint expansion fanout。
- `SprayPointH`：pointing parent next-hop 数。
- `SprayPointLevels`：waypoint 层数，默认按图规模和 degree 自动推导。
- `SprayPointSeed`：用于稳定随机排序。
- `SprayPointChoice`：
  - `packet_rr`：更像 packet-level spray。
  - `flow_hash`：同一 flow 固定 candidate。

参数建议：

```text
SprayPointP = min(4, ocs_effective_degree)
SprayPointH = min(2, ocs_effective_degree)
SprayPointLevels = auto
SprayPointChoice = packet_rr
```

这里 `ocs_effective_degree` 是逻辑 OCS 图的实际 degree；默认跨组全连接时：

```text
ocs_effective_degree = min(8N, 256M/N - 2M)
```

## 和 ECMP 的关系

ECMP 的 candidate set 一般是等价最短下一跳：

```text
candidates = shortest next hops to dst_group
```

SprayPoint 的 candidate set 是 destination-specific pointing state：

```text
source step candidates = all OCS neighbors
middle step candidates = parents[current][dst_group]
```

因此它可以作为新的 switch strategy 接进和 ECMP 同一个接口：

```cpp
Route* getNextHop(Packet& pkt, BaseQueue* ingress_port);
```

不同点是 SprayPoint 需要知道 ingress 是来自 L0 侧还是 OCS 侧，以区分 source spray 和 middle pointing。

## 当前实现状态

已有模块：

```text
htsim/sim/datacenter/huawei_ocs_spraypoint.h
htsim/sim/datacenter/huawei_ocs_spraypoint.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint_dump.cpp
```

当前已按新口径修订：

```text
1. `--coupled` 输入图节点是 logical_node(group, l1_plane, coupled_pair)。
2. 图构造由 OCS graph 层负责：degree 满跨组最大度则 complete_cross_group，否则 sparse expander。
3. dump 中 `logical_nodes_per_group` 等价于 router 内部的每 group 节点数。
4. query 的 `src_l1_eps` 会折算为 `coupled_pair = src_l1_eps / 2`。
5. 当前 8192 卡功能测试口径是 N=8,M=4：logical_nodes=128，logical_nodes_per_group=8，degree=64，因此是 sparse_expander。
```

验证点：

```text
dst_group 内节点 parent_count == 0
非 dst_group 节点有可达 parent 或 direct source neighbor
无 intra-group OCS edge
complete_cross_group 下跨组应存在直接一跳候选
```
