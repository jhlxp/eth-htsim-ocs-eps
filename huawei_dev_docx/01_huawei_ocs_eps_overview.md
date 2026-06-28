# Huawei OCS/EPS 拓扑总览

这份文档是 Huawei OCS/EPS 拓扑支持的入口。它说明当前已经实现的模型、主要源码位置、运行方式，以及后续文档的阅读顺序。

## 功能概览

Huawei 拓扑面向 8192-rank 集群建模，当前支持：

- 同一 tray 内 rank 之间走本地直连链路；
- 每个 rank 有多个外部端口，每个端口对应一个 L0/L1 plane；
- L0/L1 使用 switch-local FIB 转发，不枚举端到端完整路径；
- 跨 group 流量走 L2 OCS graph；
- L2 OCS 支持 `SprayPoint` 和 `KSP` 两种路由模式；
- 支持逐 link 负载采样，用于观察 L0、L1、OCS 各层负载均衡情况。

启用 Huawei 拓扑的方式是在 `htsim_uec` 中设置非 `off` 的 OCS 模式：

```bash
-huawei_ocs_mode spraypoint
# 或
-huawei_ocs_mode ksp
```

启用后，`main_uec.cpp` 会构造 `HuaweiTopology`，而不是使用 fat-tree 的 path enumeration。

## 源码结构

拓扑与 switch-local 转发：

```text
htsim/sim/datacenter/huawei_topology.h
htsim/sim/datacenter/huawei_topology.cpp
htsim/sim/datacenter/huawei_switch.h
htsim/sim/datacenter/huawei_switch.cpp
```

OCS graph 与路由算法：

```text
htsim/sim/datacenter/huawei_ocs_graph.h
htsim/sim/datacenter/huawei_ocs_graph.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint.h
htsim/sim/datacenter/huawei_ocs_spraypoint.cpp
htsim/sim/datacenter/huawei_ocs_ksp.h
htsim/sim/datacenter/huawei_ocs_ksp.cpp
```

调试与检查工具：

```text
htsim/sim/datacenter/huawei_ocs_graph_dump.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint_dump.cpp
htsim/sim/datacenter/huawei_ocs_ksp_dump.cpp
```

link-load 采样：

```text
htsim/sim/link_load_sampler.h
htsim/sim/link_load_sampler.cpp
```

测试与实验：

```text
tests/functional/
tests/experiments/
tests/plot/
tests/data/
```

## 拓扑模型

当前模型中，rank 是仿真端点，可以理解为一个 XPU/GPU process endpoint。

转发层级：

```text
rank
  -> 同 tray 本地直连链路，如果 src/dst 在同一个 tray
  -> 每个外部 plane 对应一个 L0 switch
  -> 同 group、同 plane 的 L1 EPS
  -> 跨 group 时进入 L2 OCS logical graph
  -> 目的 group 的 L1 EPS
  -> 目的 L0
  -> 目的 rank
```

关键映射：

```text
rank_group(rank) = rank / ranks_per_group
rank_tray(rank)  = rank / ranks_per_tray
rank_l0(rank,p)  = rank_tray(rank) * l1_planes + p
```

一个 UEC 外部端口对应一个 Huawei plane：

```text
UEC port p -> Huawei L0 plane p
```

同 tray 流量绕过外部网络：

```text
rank_src -> local direct link -> rank_dst
```

当前默认实验参数：

```text
nodes = 8192
groups = 16
ranks_per_group = 512
ranks_per_tray = 8
l1_planes = 4
l1_eps_per_l1_plane = 4
ocs_degree = 64
external_linkspeed = 100 Gbps
local_linkspeed = 800 Gbps
```

## Switch-Local 转发

Huawei 拓扑不枚举端到端完整路径。

源端 route 只到第一跳 L0：

```text
rank -> L0
```

后续每一跳由当前 switch 的本地 FIB 或 L1 OCS resolver 决定：

```text
L0: 在同 plane 的 L1 EPS 候选里选一个
L1: 如果 dst 在本 group，走下行 FIB；否则调用 OCS resolver
OCS resolver: 根据 SprayPoint 或 KSP 选择下一跳
目的 group L1/L0: 下行转发到目的 rank
```

`HuaweiSwitch` 支持：

```text
ECMP  - hash(flow_id, pathid, switch_salt)
RR    - data packet 逐包 round-robin；小控制包使用 hash
```

策略由已有的 `-strat` 控制：

```bash
-strat ecmp_host
-strat ecmp_rr
```

## OCS 路由模式

Huawei OCS 只在 L1 EPS 处理跨 group 流量时使用。

支持的模式：

```text
-huawei_ocs_mode spraypoint
-huawei_ocs_mode ksp
```

候选选择方式：

```text
-huawei_ocs_choice packet_rr
-huawei_ocs_choice flow_hash
```

`packet_rr` 使用 L1 OCS resolver 自己的 switch-local RR counter，不复用 UEC path entropy。

`flow_hash` 让同一个 flow 稳定选择同一个候选，更接近传统 ECMP hashing。

## 主要参数

拓扑参数：

```text
-nodes <N>
-huawei_ocs_groups <groups>
-huawei_ranks_per_group <ranks>
-huawei_ranks_per_tray <ranks>
-huawei_l1_planes <planes>
-huawei_l1_eps_per_l1_plane <eps>
-huawei_ocs_degree <degree>
-huawei_ocs_seed <seed>
```

路由参数：

```text
-huawei_ocs_mode off|spraypoint|ksp
-huawei_ocs_choice packet_rr|flow_hash
-huawei_spray_p <P>
-huawei_spray_h <H>
-huawei_spray_levels auto|<levels>
-huawei_ksp_k <K>
-huawei_ksp_max_hops auto|<hops>
-huawei_ksp_seed <seed>
-huawei_ksp_max_paths_per_pair <limit>
```

链路与队列：

```text
-linkspeed <Mbps>          # Huawei 外部链路
-local_linkspeed <Mbps>    # 同 tray 本地直连链路
-huawei_ocs_latency_ns <ns>
-q <packets>
```

link-load 采样：

```bash
HTSIM_LINK_LOAD_SAMPLE=1
HTSIM_LINK_LOAD_SAMPLE_US=1000
```

主要输出：

```text
output_metrics/flowsInfo.csv
output_metrics/flowsInfo_live.txt
output_metrics/link_info.csv
output_metrics/link_load_1ms.csv
output_metrics/link_load_summary.csv
output_metrics/link_load_by_layer.png
```

## 编译与测试

编译：

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target htsim_uec -j 8
```

功能测试：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_huawei_switch_local_smoke.sh
./HTSIM/tests/functional/run_huawei_ocs_dataplane_tests.sh
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

EP256 empirical 实验：

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=ksp HUAWEI_OCS_CHOICE=packet_rr KSP_K=8 \
  ./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh

HUAWEI_OCS_MODE=spraypoint HUAWEI_OCS_CHOICE=packet_rr SPRAY_P=4 SPRAY_H=2 \
  ./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

## 文档顺序

- `01_huawei_ocs_eps_overview.md`: 总览、源码位置、运行入口。
- `02_switch_local_data_plane.md`: switch-local data plane、FIB、packet metadata。
- `03_l2_ocs_graph.md`: OCS coupled graph 与 `M/N` 公式。
- `04_l2_ocs_spraypoint.md`: SprayPoint 状态、参数和转发逻辑。
- `05_l2_ocs_ksp.md`: KSP path table、packet metadata 和路径选择。
- `06_8192_cluster_topology.md`: 8192-rank 集群参数与 EP256 实验。
