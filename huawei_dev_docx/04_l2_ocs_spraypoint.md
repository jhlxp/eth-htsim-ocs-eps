# L2 OCS SprayPoint 路由

`SprayPoint` 是 Huawei 拓扑中用于跨 group L2 OCS 流量的一种路由模式。它运行在 `03_l2_ocs_graph.md` 描述的 coupled logical OCS graph 上。

实现文件：

```text
htsim/sim/datacenter/huawei_ocs_spraypoint.h
htsim/sim/datacenter/huawei_ocs_spraypoint.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint_dump.cpp
```

## 路由范围

当 L1 EPS 收到目的 rank 在其他 group 的 packet 时，才使用 SprayPoint：

```text
current_group != dst_group
```

OCS 的目标是目的 group，而不是某个具体的目的 L1 EPS：

```text
dst_set = all logical_node(dst_group, *, *)
```

packet 到达 `dst_group` 中任意 logical node 后，OCS 路由结束。后续由目的 group 内的 L1/L0 下行 FIB 送到目的 rank。

## 参数

```text
-huawei_ocs_mode spraypoint
-huawei_ocs_choice packet_rr|flow_hash
-huawei_spray_p <P>
-huawei_spray_h <H>
-huawei_spray_levels auto|<levels>
-huawei_ocs_seed <seed>
```

含义：

- `spray_p`: 构造 WP level 时使用的 expansion fanout。
- `spray_h`: 转发时保留的 parent next-hop candidate 数量。
- `spray_levels`: waypoint 层数；`auto` 会根据 graph 规模和 degree 推导。
- `huawei_ocs_choice`: 运行时从候选中选择一个 next hop 的方式。
- `huawei_ocs_seed`: graph 和稳定随机顺序的 seed。

默认实验值：

```text
spray_p = 4
spray_h = 2
spray_levels = auto
huawei_ocs_choice = packet_rr
```

## 目的端状态

router 会为每个 `dst_group` 构造一份 destination state：

```cpp
HuaweiOcsSprayPointDestinationState build_state(uint32_t dst_group) const;
```

对一个目的 group：

```text
D = dst_group 内所有 logical node
WP0 = neighbors(D) - D
WP(level+1) = selected unassigned neighbors of WP(level)
IR = unassigned neighbors of last WP level
OR = all remaining nodes
```

实现中保存：

```cpp
vector<vector<uint32_t>> waypoint_levels;
vector<uint32_t> inner_ring;
vector<uint32_t> outer_ring;
vector<vector<uint32_t>> parents;
```

## p 和 h

`p` 只用于构造 waypoint 覆盖范围：

```text
for each node in WP(level):
    select up to p unassigned neighbors into WP(level+1)
```

`h` 用于限制转发 parent 候选：

```text
WP0 -> D                  : up to h parents
WPi -> WP(i-1)            : up to h parents
IR  -> last WP level      : up to h parents
OR  -> shortest path to IR: up to h parents
fallback -> D             : up to h parents
```

因此运行时 next-hop fanout 由 `h` 控制，不由 `p` 控制。

## 源侧一次性 Spray 阶段

SprayPoint 运行时有两个阶段：

1. 源侧 spray，也就是 source spray。
2. 面向目的 group 的 pointing。

source spray 只发生一次，也就是 packet 第一次从源 group 进入 OCS 路由时：

```text
source_step = current_group == src_group && !packet.ocs_source_sprayed()
candidates = all OCS neighbors of current logical node
packet.mark_ocs_source_sprayed()
```

如果 packet 后面又回到源 group，也不会再次 source spray，而是继续走 pointing parents。

one-shot source-spray marker 保存在 `Packet` 中：

```cpp
bool ocs_source_sprayed() const;
void mark_ocs_source_sprayed();
void clear_ocs_source_sprayed();
```

## Pointing 阶段

source spray 之后，所有非目的 group 的 OCS hop 都使用当前目的 group 对应的 parents：

```text
candidates = parents[dst_group][current_logical_node]
```

不同角色的 parent 构造方式：

```text
WP0:
  parents = neighbors(current) ∩ D

WPi:
  parents = neighbors(current) ∩ WP(i-1)

IR:
  parents = neighbors(current) ∩ WP(last)

OR:
  parents = shortest-next-hop neighbors toward IR

fallback:
  parents = shortest-next-hop neighbors toward D
```

## 候选选择

运行时候选选择由 `-huawei_ocs_choice` 控制：

```text
packet_rr:
  use HuaweiSwitch::next_special_rr()
  switch-local packet round-robin over the candidate set

flow_hash:
  hash(flow_id, pathid, current_node, dst_group, seed)
  stable for a flow/entropy tuple
```

`packet_rr` 不依赖 UEC path entropy，而是使用 Huawei L1-local counter。

## 当前 N=8,M=4 行为

当前 8192-rank 实验参数：

```text
groups = 16
l1_planes = 4
l1_eps_per_l1_plane = 4
logical_nodes = 128
logical_nodes_per_group = 8
ocs_degree = 64
spray_p = 4
spray_h = 2
spray_levels = auto -> 1
```

在这组配置下，SprayPoint state 通常为：

```text
WP0 covers all non-destination logical nodes
IR = 0
OR = 0
```

也就是说，source spray 会先在 64 个 OCS neighbor 中选择一个。下一跳通常已经处在 `WP0`，随后 pointing 阶段使用最多 2 个 parent 指向目的 group。

## Data-Plane 集成

集成入口：

```cpp
HuaweiTopology::l1_special_next_hop(HuaweiSwitch* sw, Packet& pkt)
```

SprayPoint 调用：

```cpp
next_node = _spraypoint->choose_next_hop(
    current_node,
    dst_group,
    source_step,
    pkt.flow_id(),
    pkt.pathid(),
    rr,
    choice);
```

logical next node 会映射回物理 L1 EPS，并保持当前 coupled member：

```cpp
next_l1 = l1_from_logical_node(next_node, current_member)
```

## 检查 SprayPoint State

编译：

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target huawei_ocs_spraypoint_dump -j 8
```

查看 N=8,M=4：

```bash
./htsim/sim/datacenter/huawei_ocs_spraypoint_dump \
  --coupled \
  --groups 16 \
  --l1-planes 4 \
  --l1-eps-per-l1-plane 4 \
  --degree 64 \
  --ocs_expander_seed 42 \
  --spray-p 4 \
  --spray-h 2 \
  --spray-levels auto \
  --spray-seed 42
```

运行功能测试：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```
