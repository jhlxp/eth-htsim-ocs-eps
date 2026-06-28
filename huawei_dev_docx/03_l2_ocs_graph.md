# L2 OCS Graph 模型

这份文档说明 Huawei 拓扑中的 L2 OCS graph。

仿真器把 OCS fabric 建模为 coupled L1 EPS logical node 之间的图。图中的每条边表示两个跨 group logical node 之间的一条 OCS circuit。

## L1 EPS 公式

8192-rank Huawei 集群使用两个符号参数：

```text
N = 每个 group 中的 L0 domain 数
M = 每个 rank 的外部 L0/L1 plane 数
```

`M` 和 `N` 不改变总 rank 数：

```text
groups = 128 / N
ranks_per_group = 64N
total_ranks = (128 / N) * 64N = 8192
```

L1 EPS 总数为：

```text
l1_eps_total = groups * M * 4
             = (128 / N) * M * 4
             = 512M / N
```

OCS 相关公式：

```text
physical_ocs_switches = 8N
ocs_ports_per_switch = 512M / N
l1_uplinks_per_eps = 8N
```

端口账能对齐：

```text
L1 uplink ports = (512M/N) * (8N) = 4096M
OCS ports       = (8N) * (512M/N) = 4096M
```

## Coupled Logical Node

L1 EPS 进入 OCS graph 前先按相邻同 plane EPS 合口：

```text
EPS0 + EPS1 -> coupled_pair 0
EPS2 + EPS3 -> coupled_pair 1
```

每个 group、每个 L1 plane：

```text
l1_eps_per_l1_plane = 4
coupled_pairs_per_plane = 2
```

OCS routing graph 的节点为：

```text
logical_node = (group, l1_plane, coupled_pair)
```

节点数：

```text
logical_nodes_total = l1_eps_total / 2 = 256M / N
logical_nodes_per_group = 2M
```

实现文件：

```text
htsim/sim/datacenter/huawei_ocs_graph.h
htsim/sim/datacenter/huawei_ocs_graph.cpp
```

关键 helper：

```cpp
huawei_ocs_coupled_endpoint_id(...)
huawei_ocs_decode_coupled_endpoint(...)
huawei_ocs_coupled_logical_node_id(...)
huawei_ocs_decode_coupled_logical_node(...)
huawei_ocs_coupled_logical_nodes_per_group(...)
```

## 跨 Group Degree

OCS 不连接同 group 内的 logical node。同 group 流量走 L0/L1 electrical fabric。

定义：

```text
Q = 256M / N      # logical node 总数
S = 2M            # 每个 group 的 logical node 数
K = 8N            # 物理 OCS round/switch 数
```

最大有效跨 group degree：

```text
D_cross = Q - S = 256M/N - 2M
```

仿真器使用：

```text
ocs_degree = min(8N, 256M/N - 2M)
```

当：

```text
ocs_degree == D_cross
```

OCS graph 是 cross-group complete。否则是 sparse regular expander。

## 全连接条件

全节点 full mesh 条件：

```text
8N >= 256M/N - 1
```

跨 group full mesh 条件：

```text
8N >= 256M/N - 2M
```

实现采用跨 group 条件，因为组内 OCS edge 会被跳过。

## Graph 构造

graph builder：

```cpp
build_huawei_ocs_coupled_template(
    groups,
    l1_planes,
    l1_eps_per_l1_plane,
    degree,
    ocs_seed)
```

构造性质：

- 节点为 `logical_node(group, l1_plane, coupled_pair)`；
- 每条边连接两个不同 group；
- 每个节点 degree 等于 `degree`；
- 每个 OCS round 是 logical node 上的一个 matching；
- 固定 seed 下结果确定；
- builder 会检查 degree、奇偶性、连通性和无组内边。

生成的 graph 保存为：

```cpp
struct HuaweiOcsGraph {
    uint32_t nodes;
    uint32_t degree;
    uint32_t seed;
    vector<pair<uint32_t,uint32_t>> edges;
    vector<uint32_t> edge_ocs;
    vector<vector<uint32_t>> adjacency;
};
```

`edge_ocs[i]` 表示 `edges[i]` 来自哪个 OCS round。

## 示例：N=8, M=4

当前实验使用 `N=8, M=4`：

```text
groups = 16
ranks_per_group = 512
l1_eps_total = 512 * 4 / 8 = 256
logical_nodes_total = 128
logical_nodes_per_group = 8
physical_ocs_switches = 64
cross_group_full_degree = 128 - 8 = 120
ocs_degree = min(64, 120) = 64
```

因此当前 OCS graph 是 degree-64 的 sparse cross-group regular graph，不是 cross-group complete。

## Graph Dump 工具

编译 dump 工具：

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target huawei_ocs_graph_dump -j 8
```

查看当前 N=8,M=4 graph：

```bash
./htsim/sim/datacenter/huawei_ocs_graph_dump \
  --coupled \
  --groups 16 \
  --l1-planes 4 \
  --l1-eps-per-l1-plane 4 \
  --degree 64 \
  --ocs_expander_seed 42
```

功能测试会自动检查同样的 graph 性质：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```
