# 8192-Rank Huawei Cluster Configuration

This document records the parameterized 8192-rank Huawei topology used by the experiments.

## Scale

The model uses:

```text
8192 ranks
8 ranks per tray
64 ranks per L0 domain
128 L0 domains total
```

`N` changes group size. `M` changes the number of rank external planes/ports. Neither changes the total rank count.

```text
groups = 128 / N
ranks_per_group = 64N
total_ranks = groups * ranks_per_group = 8192
```

## Plane and L1 EPS Count

`M` is the number of external L0/L1 planes:

```text
l1_planes = M
UEC ports per rank = M
```

Each group has 4 L1 EPS per plane:

```text
l1_eps_per_group = 4M
l1_eps_total = (128/N) * 4M = 512M/N
```

This is the L1 EPS formula used by the simulator.

## OCS Counts

Physical OCS rounds/switches:

```text
physical_ocs_switches = 8N
```

L1 EPS uplinks:

```text
l1_uplinks_per_eps = 8N
```

OCS ports per switch:

```text
ocs_ports_per_switch = 512M/N
```

Port accounting:

```text
L1 uplink ports = (512M/N) * (8N) = 4096M
OCS ports       = (8N) * (512M/N) = 4096M
```

## Coupled OCS Logical Nodes

Each pair of same-plane L1 EPS shares OCS ports:

```text
EPS0/EPS1 -> coupled_pair 0
EPS2/EPS3 -> coupled_pair 1
```

Logical OCS nodes:

```text
logical_node = (group, l1_plane, coupled_pair)
logical_nodes_total = 256M/N
logical_nodes_per_group = 2M
```

The simulator builds OCS routing over these logical nodes.

## Cross-Group OCS Degree

The useful cross-group maximum degree is:

```text
D_cross = logical_nodes_total - logical_nodes_per_group
        = 256M/N - 2M
```

The default effective degree is:

```text
ocs_degree = min(8N, 256M/N - 2M)
```

If:

```text
8N >= 256M/N - 2M
```

the OCS graph is cross-group complete. Otherwise it is a sparse expander.

## M=4 Sweep

Current large-cluster tests use `M=4`. The table below shows how `N` changes the group and OCS graph shape while total ranks remain 8192.

| N | groups | ranks/group | L1 EPS total | logical nodes Q | logical/group S | OCS rounds 8N | cross degree Q-S | complete cross-group? |
|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| 1 | 128 | 64 | 2048 | 1024 | 8 | 8 | 1016 | No |
| 2 | 64 | 128 | 1024 | 512 | 8 | 16 | 504 | No |
| 4 | 32 | 256 | 512 | 256 | 8 | 32 | 248 | No |
| 8 | 16 | 512 | 256 | 128 | 8 | 64 | 120 | No |
| 16 | 8 | 1024 | 128 | 64 | 8 | 128 | 56 | Yes |
| 32 | 4 | 2048 | 64 | 32 | 8 | 256 | 24 | Yes |
| 64 | 2 | 4096 | 32 | 16 | 8 | 512 | 8 | Yes |

## Current Experiment Configuration

The default EP256 experiment uses:

```text
N = 8
M = 4
nodes = 8192
groups = 16
ranks_per_group = 512
ranks_per_tray = 8
l1_planes = 4
l1_eps_per_l1_plane = 4
logical_nodes = 128
logical_nodes_per_group = 8
ocs_degree = 64
cross_group_full_degree = 120
```

Therefore:

```text
OCS graph = sparse cross-group regular expander
```

Link speeds:

```text
rank <-> L0          100 Gbps
L0 <-> L1            100 Gbps
L1 <-> OCS/L1        100 Gbps
same-tray direct     800 Gbps
```

Queue:

```text
default experiment queue = 100 packets
```

## EP256 Rank Placement

The empirical all-to-all experiment generates 256 active EP ranks and places them into the 8192-rank topology.

Default placement:

```text
rank_placement = strided
rank_offset = 0
placement_seed = 42
```

Traffic generation:

```text
tokens_per_rank = 4096
topk = 8
hidden_size = 7168
dtype_bytes = 2
moe_layers = 1
include_combine = 0
```

The input distribution is:

```text
tests/data/empirical_pooled_distribution.csv
```

It is derived from pooled `decode_*.csv` expert hotness samples and committed as a small reproducible distribution file.

## Running the Current Experiments

KSP:

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=ksp \
HUAWEI_OCS_CHOICE=packet_rr \
KSP_K=8 \
./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

SprayPoint:

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=spraypoint \
HUAWEI_OCS_CHOICE=packet_rr \
SPRAY_P=4 \
SPRAY_H=2 \
./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

Outputs are written under:

```text
tests/log/run_<timestamp>_huawei_<strategy>_<ocs_mode>_<choice>_<nodes>nodes_ep256_empirical/
```

Key result files:

```text
run_config.txt
htsim_<mode>_<choice>_<strategy>.log
output_metrics/flowsInfo.csv
output_metrics/link_info.csv
output_metrics/link_load_1ms.csv
output_metrics/link_load_by_layer.png
```
