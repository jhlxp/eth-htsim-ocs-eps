# 8192-Rank Huawei 集群配置

这份文档记录当前实验使用的 8192-rank Huawei 拓扑参数。

## 集群规模

模型使用：

```text
8192 ranks
8 ranks per tray
64 ranks per L0 domain
128 L0 domains total
```

`N` 改变 group 规模，`M` 改变 rank 外部 plane/port 数量。二者不改变总 rank 数。

```text
groups = 128 / N
ranks_per_group = 64N
total_ranks = groups * ranks_per_group = 8192
```

## Plane 与 L1 EPS 数量

`M` 是外部 L0/L1 plane 数：

```text
l1_planes = M
UEC ports per rank = M
```

每个 group、每个 plane 有 4 个 L1 EPS：

```text
l1_eps_per_group = 4M
l1_eps_total = (128/N) * 4M = 512M/N
```

这是仿真器使用的 L1 EPS 公式。

## OCS 数量

物理 OCS round/switch 数：

```text
physical_ocs_switches = 8N
```

每个 L1 EPS 的 uplink：

```text
l1_uplinks_per_eps = 8N
```

每个 OCS switch 的端口：

```text
ocs_ports_per_switch = 512M/N
```

端口账：

```text
L1 uplink ports = (512M/N) * (8N) = 4096M
OCS ports       = (8N) * (512M/N) = 4096M
```

## Coupled OCS Logical Nodes

同 plane 的相邻 L1 EPS 成对合口：

```text
EPS0/EPS1 -> coupled_pair 0
EPS2/EPS3 -> coupled_pair 1
```

OCS logical node：

```text
logical_node = (group, l1_plane, coupled_pair)
logical_nodes_total = 256M/N
logical_nodes_per_group = 2M
```

仿真器在这些 logical node 上构造 OCS routing graph。

## Cross-Group OCS Degree

有效的最大跨 group degree：

```text
D_cross = logical_nodes_total - logical_nodes_per_group
        = 256M/N - 2M
```

默认有效 degree：

```text
ocs_degree = min(8N, 256M/N - 2M)
```

如果：

```text
8N >= 256M/N - 2M
```

OCS graph 是 cross-group complete。否则是 sparse expander。

## M=4 参数表

当前大规模测试使用 `M=4`。下表展示不同 `N` 下 group 与 OCS graph 的变化，总 rank 数始终为 8192。

| N | groups | ranks/group | L1 EPS total | logical nodes Q | logical/group S | OCS rounds 8N | cross degree Q-S | cross-group complete? |
|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| 1 | 128 | 64 | 2048 | 1024 | 8 | 8 | 1016 | No |
| 2 | 64 | 128 | 1024 | 512 | 8 | 16 | 504 | No |
| 4 | 32 | 256 | 512 | 256 | 8 | 32 | 248 | No |
| 8 | 16 | 512 | 256 | 128 | 8 | 64 | 120 | No |
| 16 | 8 | 1024 | 128 | 64 | 8 | 128 | 56 | Yes |
| 32 | 4 | 2048 | 64 | 32 | 8 | 256 | 24 | Yes |
| 64 | 2 | 4096 | 32 | 16 | 8 | 512 | 8 | Yes |

## 当前实验配置

默认 EP256 实验使用：

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

因此：

```text
OCS graph = sparse cross-group regular expander
```

链路速率：

```text
rank <-> L0          100 Gbps
L0 <-> L1            100 Gbps
L1 <-> OCS/L1        100 Gbps
same-tray direct     800 Gbps
```

队列：

```text
default experiment queue = 100 packets
```

## EP256 Rank Placement

empirical all-to-all 实验生成 256 个 active EP rank，并把它们放入 8192-rank 拓扑。

默认 placement：

```text
rank_placement = strided
rank_offset = 0
placement_seed = 42
```

流量生成：

```text
tokens_per_rank = 4096
topk = 8
hidden_size = 7168
dtype_bytes = 2
moe_layers = 1
include_combine = 0
```

输入分布：

```text
tests/data/empirical_pooled_distribution.csv
```

该文件由 pooled `decode_*.csv` expert hotness 样本生成，是一个较小的可复现实验分布文件。

## 运行当前实验

KSP：

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=ksp \
HUAWEI_OCS_CHOICE=packet_rr \
KSP_K=8 \
./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

SprayPoint：

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=spraypoint \
HUAWEI_OCS_CHOICE=packet_rr \
SPRAY_P=4 \
SPRAY_H=2 \
./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

输出目录：

```text
tests/log/run_<timestamp>_huawei_<strategy>_<ocs_mode>_<choice>_<nodes>nodes_ep256_empirical/
```

关键结果文件：

```text
run_config.txt
htsim_<mode>_<choice>_<strategy>.log
output_metrics/flowsInfo.csv
output_metrics/link_info.csv
output_metrics/link_load_1ms.csv
output_metrics/link_load_by_layer.png
```
