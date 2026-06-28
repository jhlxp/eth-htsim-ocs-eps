# L2 OCS Graph Model

This document describes the L2 OCS graph used by the Huawei topology implementation.

The simulator builds the OCS fabric as a graph over coupled L1 EPS logical nodes. Each graph edge represents one OCS circuit between two cross-group logical nodes.

## Corrected L1 EPS Formula

The 8192-rank Huawei cluster uses two symbolic parameters:

```text
N = number of L0 domains per group
M = number of external L0/L1 planes per rank
```

The total rank count does not change with `M` or `N`:

```text
groups = 128 / N
ranks_per_group = 64N
total_ranks = (128 / N) * 64N = 8192
```

The L1 EPS count is:

```text
l1_eps_total = groups * M * 4
             = (128 / N) * M * 4
             = 512M / N
```

The OCS-related formulas used by the implementation are:

```text
physical_ocs_switches = 8N
ocs_ports_per_switch = 512M / N
l1_uplinks_per_eps = 8N
```

Port accounting closes:

```text
L1 uplink ports = (512M/N) * (8N) = 4096M
OCS ports       = (8N) * (512M/N) = 4096M
```

## Coupled Logical Nodes

L1 EPS are coupled in pairs before entering the OCS graph:

```text
EPS0 + EPS1 -> coupled_pair 0
EPS2 + EPS3 -> coupled_pair 1
```

For each group and each L1 plane:

```text
l1_eps_per_l1_plane = 4
coupled_pairs_per_plane = 2
```

The OCS routing graph uses logical nodes:

```text
logical_node = (group, l1_plane, coupled_pair)
```

Node counts:

```text
logical_nodes_total = l1_eps_total / 2 = 256M / N
logical_nodes_per_group = 2M
```

The source files implement this mapping in:

```text
htsim/sim/datacenter/huawei_ocs_graph.h
htsim/sim/datacenter/huawei_ocs_graph.cpp
```

Key helpers:

```cpp
huawei_ocs_coupled_endpoint_id(...)
huawei_ocs_decode_coupled_endpoint(...)
huawei_ocs_coupled_logical_node_id(...)
huawei_ocs_decode_coupled_logical_node(...)
huawei_ocs_coupled_logical_nodes_per_group(...)
```

## Cross-Group Degree

OCS links do not connect logical nodes in the same group. Same-group traffic uses the electrical L0/L1 fabric.

Let:

```text
Q = 256M / N      # total logical nodes
S = 2M            # logical nodes per group
K = 8N            # physical OCS rounds/switches
```

The maximum useful cross-group degree is:

```text
D_cross = Q - S = 256M/N - 2M
```

The simulator uses:

```text
ocs_degree = min(8N, 256M/N - 2M)
```

When:

```text
ocs_degree == D_cross
```

the graph is cross-group complete. Otherwise it is a sparse regular expander.

## Full-Connectivity Checks

All-node full mesh condition:

```text
8N >= 256M/N - 1
```

Cross-group full mesh condition:

```text
8N >= 256M/N - 2M
```

The implementation uses the cross-group condition because intra-group OCS edges are intentionally skipped.

## Graph Construction

The graph builder is:

```cpp
build_huawei_ocs_coupled_template(
    groups,
    l1_planes,
    l1_eps_per_l1_plane,
    degree,
    ocs_seed)
```

Properties:

- nodes are `logical_node(group, l1_plane, coupled_pair)`;
- every edge connects two different groups;
- each node has exactly `degree` neighbors;
- each OCS round is a matching over logical nodes;
- the graph is deterministic for a fixed seed;
- the builder validates degree, parity, connectivity, and no intra-group edges.

The generated graph is stored as:

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

`edge_ocs[i]` records which OCS round produced `edges[i]`.

## Example: N=8, M=4

Current experiments use `N=8, M=4`:

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

So the current OCS graph is sparse, degree-64, and cross-group regular. It is not cross-group complete.

## Graph Dump

Build the dump binary:

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target huawei_ocs_graph_dump -j 8
```

Inspect the current N=8,M=4 graph:

```bash
./htsim/sim/datacenter/huawei_ocs_graph_dump \
  --coupled \
  --groups 16 \
  --l1-planes 4 \
  --l1-eps-per-l1-plane 4 \
  --degree 64 \
  --ocs_expander_seed 42
```

The functional test validates the same properties automatically:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```
