# Huawei OCS/EPS Topology Support

This document is the entry point for the Huawei OCS/EPS topology support added to HTSIM. It describes the implemented model, the main source files, and how packets are forwarded through the simulator.

## Feature Summary

The Huawei topology models an 8192-rank cluster with:

- server/tray-local direct links between ranks in the same tray;
- multiple rank external ports, one per L0/L1 plane;
- switch-local L0/L1 forwarding, using the same style as HTSIM fat-tree switches;
- an L2 OCS cross-group fabric built from coupled L1 EPS logical nodes;
- two OCS routing policies: SprayPoint and KSP;
- per-link load sampling for topology-layer utilization analysis.

Huawei mode is selected by passing a non-`off` OCS mode to `htsim_uec`:

```bash
-huawei_ocs_mode spraypoint
# or
-huawei_ocs_mode ksp
```

When Huawei mode is enabled, `main_uec.cpp` constructs `HuaweiTopology` instead of enumerating fat-tree paths.

## Source Layout

Core topology and switch-local forwarding:

```text
htsim/sim/datacenter/huawei_topology.h
htsim/sim/datacenter/huawei_topology.cpp
htsim/sim/datacenter/huawei_switch.h
htsim/sim/datacenter/huawei_switch.cpp
```

OCS graph and routing policies:

```text
htsim/sim/datacenter/huawei_ocs_graph.h
htsim/sim/datacenter/huawei_ocs_graph.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint.h
htsim/sim/datacenter/huawei_ocs_spraypoint.cpp
htsim/sim/datacenter/huawei_ocs_ksp.h
htsim/sim/datacenter/huawei_ocs_ksp.cpp
```

Debug and inspection binaries:

```text
htsim/sim/datacenter/huawei_ocs_graph_dump.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint_dump.cpp
htsim/sim/datacenter/huawei_ocs_ksp_dump.cpp
```

Link-load sampling:

```text
htsim/sim/link_load_sampler.h
htsim/sim/link_load_sampler.cpp
```

Tests and experiments:

```text
tests/functional/
tests/experiments/
tests/plot/
tests/data/
```

## Topology Model

The current Huawei model uses ranks as simulation endpoints. A rank is equivalent to one XPU/GPU process endpoint.

Implemented hierarchy:

```text
rank
  -> tray-local direct link, if src and dst are in the same tray
  -> L0 switch for each external plane
  -> L1 EPS switch in the same group and plane
  -> L2 OCS logical graph for cross-group traffic
  -> destination-group L1 EPS
  -> destination L0
  -> destination rank
```

Important mapping rules:

```text
rank_group(rank) = rank / ranks_per_group
rank_tray(rank)  = rank / ranks_per_tray
rank_l0(rank,p)  = rank_tray(rank) * l1_planes + p
```

One external rank port maps to one plane:

```text
UEC port p -> Huawei L0 plane p
```

Same-tray traffic bypasses the external network:

```text
rank_src -> local direct link -> rank_dst
```

The default experiment uses:

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

## Switch-Local Forwarding

Huawei forwarding is not full-path enumeration.

The source rank route only reaches the first L0 switch:

```text
rank -> L0
```

Every later hop is selected by the current switch from its local FIB:

```text
L0: choose one same-plane L1 EPS candidate
L1: choose group-local downlink, or call the OCS resolver for cross-group traffic
OCS resolver: choose SprayPoint or KSP next hop
destination L1/L0: route down to the destination rank
```

`HuaweiSwitch` supports:

```text
ECMP  - hash(flow_id, pathid, switch_salt)
RR    - packet-level round-robin for data packets; small control packets use hash
```

The selected strategy is controlled by the existing `-strat` argument:

```bash
-strat ecmp_host
-strat ecmp_rr
```

## OCS Routing Modes

Huawei OCS routing is only used at L1 when the destination rank is in a different group.

Supported modes:

```text
-huawei_ocs_mode spraypoint
-huawei_ocs_mode ksp
```

Supported OCS candidate selection:

```text
-huawei_ocs_choice packet_rr
-huawei_ocs_choice flow_hash
```

`packet_rr` uses a switch-local RR counter at the L1 OCS resolver. It does not reuse UEC path entropy.

`flow_hash` keeps the same flow on a stable candidate, closer to traditional ECMP hashing.

## Main CLI Parameters

Topology:

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

Routing:

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

Link model:

```text
-linkspeed <Mbps>          # external Huawei links
-local_linkspeed <Mbps>    # same-tray direct links
-huawei_ocs_latency_ns <ns>
-q <packets>
```

Link-load sampling:

```bash
HTSIM_LINK_LOAD_SAMPLE=1
HTSIM_LINK_LOAD_SAMPLE_US=1000
```

Outputs:

```text
output_metrics/flowsInfo.csv
output_metrics/flowsInfo_live.txt
output_metrics/link_info.csv
output_metrics/link_load_1ms.csv
output_metrics/link_load_summary.csv
output_metrics/link_load_by_layer.png
```

## Build and Test

Build:

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target htsim_uec -j 8
```

Run functional tests:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_huawei_switch_local_smoke.sh
./HTSIM/tests/functional/run_huawei_ocs_dataplane_tests.sh
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

Run the EP256 empirical experiment:

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=ksp HUAWEI_OCS_CHOICE=packet_rr KSP_K=8 \
  ./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh

HUAWEI_OCS_MODE=spraypoint HUAWEI_OCS_CHOICE=packet_rr SPRAY_P=4 SPRAY_H=2 \
  ./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

## Document Map

- `01_huawei_ocs_eps_overview.md`: Entry point and source layout.
- `02_switch_local_data_plane.md`: Switch-local forwarding and packet metadata.
- `03_l2_ocs_graph.md`: OCS coupled graph and `M/N` formulas.
- `04_l2_ocs_spraypoint.md`: SprayPoint forwarding state and parameters.
- `05_l2_ocs_ksp.md`: KSP path table, packet metadata, and selection.
- `06_8192_cluster_topology.md`: 8192-rank cluster parameter table.
