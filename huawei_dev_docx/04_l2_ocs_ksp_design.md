# Huawei L2-OCS KSP 设计

这份文档只描述 `L2Mode=ocs` 下的 KSP 路由。基础 OCS 拓扑、L1 EPS 修订公式、合口逻辑节点和全连接判断见 `02_l2_ocs_design.md`。

## 目标语义

KSP 在 Huawei L2-OCS 中是完整路径策略，而不是普通 next-hop ECMP：

```text
src logical OCS node
  -> select one complete KSP path
  -> tag packet with OCS KSP metadata
  -> follow selected path hop by hop
  -> any logical OCS node in dst_group
  -> group-local downlink
```

OCS 层的目标是 `dst_group`，不是具体 `dst_l1_eps`。一旦当前 logical node 属于 `dst_group`，OCS 段结束。

## 图粒度

KSP 运行在合口后的逻辑 OCS 图上：

```text
node = logical_node(group, l1_plane, coupled_pair)
edge = cross-group OCS circuit
```

节点数量：

```text
logical_nodes = 256M/N
logical_nodes_per_group = 2M
```

物理 L1 EPS 先映射到 logical node：

```text
coupled_pair = l1_eps_index / 2
logical_node = (group, l1_plane, coupled_pair)
```

## 和全连接 OCS 的关系

如果 `OcsTopology=complete_cross_group`，任意跨组 logical node 都有直接 circuit：

```text
src_group != dst_group => path length 1 exists
```

此时 KSP 表里最短路径基本都是一跳：

```text
src -> dst_group_any_node
```

KSP 仍然可以用于：

```text
1. 在多个直接 dst_group 入口之间做 flow_hash / packet_rr。
2. 在有冗余 OCS 轮次时选择不同入口。
3. 在 OcsTopology=sparse_expander 时做真正多跳路径。
```

因此 KSP 的核心价值在稀疏 OCS 或非全连接 OCS 配置里。

## KSP 和 next-hop 的区别

KSP 本身只是候选路径生成：

```text
src_logical_node -> dst_group 的 K 条完整路径
```

真正使用这些路径时推荐严格路径级 KSP：

```text
flow/flowlet/packet 先选完整 path_id
中间节点沿同一条 path_id 转发
```

不建议第一版使用 local-KSP approximation，因为每个中间节点独立重选会偏离完整 path 语义。

## 目的地集合

路径终点是目的 group 内任意 logical node：

```text
dst_set(group g) = all logical_node(g, *, *)
```

路径表：

```text
paths[src_logical_node][dst_group] = [
  [src_logical_node, ..., dst_group_logical_node],
  ...
]
```

候选路径按 hop 数升序、稳定 tie-break 排序，保留前 `OcsKspK` 条。

## Packet Metadata

为了让中间 L1 EPS 知道包属于哪条完整 KSP path，建议新增 L2-OCS KSP 专属字段，不复用现有 `pathid`。

建议字段：

```cpp
bool _has_ocs_ksp_route;
uint32_t _ocs_ksp_src_node;
uint32_t _ocs_ksp_dst_group;
uint32_t _ocs_ksp_path_id;
```

建议接口：

```cpp
bool has_ocs_ksp_route() const;
void clear_ocs_ksp_route();
void set_ocs_ksp_route(
    uint32_t src_node,
    uint32_t dst_group,
    uint32_t path_id);

uint32_t ocs_ksp_src_node() const;
uint32_t ocs_ksp_dst_group() const;
uint32_t ocs_ksp_path_id() const;
```

不需要单独保存 `hop_index`。中间节点知道自己的 `current_logical_node`，可以直接查：

```text
next_hop(src_node, dst_group, path_id, current_logical_node)
```

## 转发流程

### Source L1 EPS

当包从 L0/rank 侧进入 L1，且目的 rank 在其他 group：

```text
src_node = current physical L1 EPS 映射到的 logical_node
dst_group = rank_to_group(pkt.dst())
path_id = choose_path(src_node, dst_group, pkt)
pkt.set_ocs_ksp_route(src_node, dst_group, path_id)
next = table.next_hop(src_node, dst_group, path_id, src_node)
```

### Middle L1 EPS

当包从 OCS 侧进入 L1，且当前 group 不是 `dst_group`：

```text
assert(pkt.has_ocs_ksp_route())
src_node = pkt.ocs_ksp_src_node()
dst_group = pkt.ocs_ksp_dst_group()
path_id = pkt.ocs_ksp_path_id()
current = current physical L1 EPS 映射到的 logical_node
next = table.next_hop(src_node, dst_group, path_id, current)
```

### Destination Group L1 EPS

当包到达 `dst_group` 内任意 logical node：

```text
pkt.clear_ocs_ksp_route()
停止 OCS 转发
进入组内下行：L1 -> L0 -> tray/rank
```

## Path Selection 粒度

KSP 不规定 flow 级还是 packet 级。KSP 只生成候选 path，粒度由 path selection 决定。

建议支持：

```text
OcsKspChoice flow_hash | packet_hash | packet_rr | flowlet_hash
```

第一版建议：

```text
OcsKspChoice flow_hash
```

原因：

- flow-level KSP 最接近常规 KSP/ECMP 工程语义。
- packet-level KSP 可能跨多条完整 path 分包，引入乱序。
- flowlet-level KSP 后续可以接入，但需要 flowlet 边界检测。

## 参数

建议参数：

```text
OcsRouting ksp
OcsKspK 8
OcsKspMaxHops auto | N
OcsKspSeed 42
OcsKspChoice flow_hash | packet_hash | packet_rr | flowlet_hash
```

含义：

- `OcsKspK`：每个 `(src_logical_node, dst_group)` 保留 K 条候选完整 path。
- `OcsKspMaxHops`：可选最大 OCS hop 数，防止路径过长。
- `OcsKspSeed`：稳定 tie-break 盐。
- `OcsKspChoice`：源 L1 选择 path 的粒度。

## 路径生成算法

参考 `reference/random_graph.py`：

```python
list(islice(nx.shortest_simple_paths(G, src, dst), K))
```

Huawei 版本是 multi-target Yen-style KSP：

```text
for each src_node:
  for each dst_group != src_group:
    first_path = BFS shortest path from src_node to any node in dst_group
    accepted = [first_path]
    candidates = []
    while accepted.size < K:
      for each spur position in previous accepted path:
        root = previous_path[0..spur]
        ban edges that would recreate existing accepted paths with same root
        ban root nodes before spur to keep path simple
        spur_path = BFS shortest path from spur to any node in dst_group
        candidates.add(root + spur_path)
      accepted.add(best candidate by hop_count, stable_hash, lexicographic)
```

这样不需要枚举全部 simple paths。`OcsKspMaxHops` 限制搜索深度，`OcsKspK` 控制每个 `(src_node, dst_group)` 保留的 path 数。

## Sanity Check

每条 path 必须满足：

```text
path.front() == src_node
group(path.back()) == dst_group
for every adjacent pair (u, v):
    edge(u, v) exists in logical OCS graph
for every node in path before last:
    group(node) != dst_group
no repeated node in a path
```

如果某个 `(src_node, dst_group)` 不可达，应 fail fast 或在 dump 中明确输出。

## Dump 输出

建议 CSV：

```text
src_node,src_group,src_l1_plane,src_coupled_pair,dst_group,path_id,hop_count,path_nodes
```

示例：

```text
0,0,0,0,3,1,3,0;5;6;7
```

## 当前实现需要对齐的点

已有模块：

```text
htsim/sim/datacenter/huawei_ocs_ksp.h
htsim/sim/datacenter/huawei_ocs_ksp.cpp
htsim/sim/datacenter/huawei_ocs_ksp_dump.cpp
```

当前已按新口径修订：

```text
1. `--coupled` 输入图节点是 logical_node(group, l1_plane, coupled_pair)。
2. 图构造由 OCS graph 层负责：degree 满跨组最大度则 complete_cross_group，否则 sparse expander。
3. query 的 `src_l1_eps` 会折算为 `coupled_pair = src_l1_eps / 2`。
4. complete_cross_group 下优先得到一跳 KSP path。
5. sparse_expander 下可以看到多跳 KSP path。
6. 当前 8192 卡功能测试口径是 N=8,M=4：logical_nodes=128，logical_nodes_per_group=8，degree=64，因此是 sparse_expander。
```

## 真实网络类比

这个标签语义类似 SR/MPLS/VXLAN metadata：

```text
源端选择完整 overlay path
中间节点根据 packet metadata 查下一跳
到达目的域后清理 metadata
```

HTSIM 中先作为仿真字段实现，不要求对应真实协议栈逐字节一致。
