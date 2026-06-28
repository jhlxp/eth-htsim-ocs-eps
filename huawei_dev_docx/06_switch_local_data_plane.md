# Switch-Local Huawei Data Plane

This document describes the implemented Huawei switch-local data plane.

The key rule is:

```text
source routes only reach the first L0 switch;
all later hops are selected by HuaweiSwitch local FIBs or by the L1 OCS resolver.
```

No full end-to-end path list is generated for Huawei topology.

## Data-Plane Objects

Topology:

```cpp
HuaweiTopology
```

Switch:

```cpp
HuaweiSwitch
```

OCS special resolver:

```cpp
HuaweiTopology::l1_special_next_hop(...)
```

UEC connection setup:

```cpp
HuaweiTopology::connect_endpoints(src, dst, uec_src, uec_snk, start_time)
```

## Route Setup

For same-tray traffic:

```text
rank_src -> local direct 800G link -> rank_dst
```

The same local route is attached to all UEC ports, but it does not enter L0/L1/OCS.

For external traffic, each UEC port maps to one plane:

```text
port p:
  source route = rank_src -> L0(src,p)
  reverse route = rank_dst -> L0(dst,p)
```

The first route fragment ends at an L0 switch. Later hops are selected by switch-local forwarding.

## L0 FIB

For each active destination, source-side L0 switches install up-routes:

```text
L0(src, plane p) -> all L1 EPS in src_group and plane p
```

Candidate count:

```text
l1_eps_per_l1_plane
```

With the current experiment:

```text
l1_eps_per_l1_plane = 4
```

When `-strat ecmp_rr` is used, data packets are round-robined across those candidates.

## L1 Group-Local FIB

For every destination rank, the destination group L1 switches install down-routes:

```text
L1(dst_group, plane p, eps e) -> L0(dst,p)
```

If a packet arrives at any L1 switch in the destination group, the OCS resolver returns `nullptr`, and normal FIB lookup routes down toward the destination L0 and rank.

## L1 Cross-Group OCS Resolver

If an L1 switch sees:

```text
current_group != dst_group
```

it calls the OCS resolver:

```cpp
HuaweiTopology::l1_special_next_hop(...)
```

The resolver maps the physical L1 EPS into a coupled logical node:

```text
current_node = logical_node(group, l1_plane, coupled_pair)
```

Then it chooses one OCS next logical node using the configured mode:

```text
spraypoint
ksp
```

The selected logical node is mapped back to a physical L1 EPS while preserving the coupled member:

```text
next_l1 = l1_from_logical_node(next_node, current_member)
```

The physical link is:

```text
L1(current) -> L1(next)
```

It is recorded as layer:

```text
huawei_l1_ocs
```

## HuaweiSwitch Strategy

`HuaweiSwitch` mirrors the useful local behavior of `FatTreeSwitch` without using fat-tree pod/tier formulas.

Supported strategies:

```text
ECMP
RR
```

Mapping from CLI:

```text
-strat ecmp_host -> HuaweiSwitch::ECMP
-strat ecmp_rr   -> HuaweiSwitch::RR
```

Selection rules:

```text
ECMP:
  hash(flow_id, pathid, switch_salt)

RR:
  if packet size < 128 bytes:
      hash(flow_id, pathid, switch_salt)
  else:
      switch-local packet round-robin
```

OCS `packet_rr` uses a separate `HuaweiSwitch::next_special_rr()` counter so OCS packet spray is independent of UEC path entropy.

## Packet Metadata

Huawei OCS adds two pieces of packet state.

SprayPoint:

```cpp
bool ocs_source_sprayed() const;
void mark_ocs_source_sprayed();
void clear_ocs_source_sprayed();
```

This enforces source-only spray.

KSP:

```cpp
bool has_ocs_ksp_route() const;
void set_ocs_ksp_route(src_node, dst_group, path_id);
void clear_ocs_ksp_route();
```

This lets intermediate L1 switches continue along the same complete KSP path.

Both metadata groups are cleared when a packet is reinitialized.

## Link Names and Layers

The topology creates named queue/pipe links. The link-load sampler parses those names into layers:

```text
LOCAL_SRCx->DSTy        -> huawei_local_direct
SRCx->L0_i              -> huawei_host_l0 up
L0_i->DSTx              -> huawei_host_l0 down
L0_i->L1_j              -> huawei_l0_l1 up
L1_j->L0_i              -> huawei_l0_l1 down
L1_i->L1_j              -> huawei_l1_ocs cross
```

Sample output:

```text
output_metrics/link_info.csv
output_metrics/link_load_1ms.csv
output_metrics/link_load_summary.csv
output_metrics/link_load_by_layer.png
```

## Runtime Logging

Huawei mode prints structured configuration blocks:

```text
#----------- HUAWEI TOPOLOGY begin ------------
#----------- HUAWEI TOPOLOGY END ------------

#----------- HUAWEI ROUTING begin ------------
#----------- HUAWEI ROUTING END ------------

#----------- HUAWEI LINK_QUEUE begin ------------
#----------- HUAWEI LINK_QUEUE END ------------

#----------- HUAWEI OUTPUT begin ------------
#----------- HUAWEI OUTPUT END ------------
```

These blocks include input parameters and derived statistics such as:

```text
groups
ranks_per_group
ranks_per_tray
l1_planes_ports
l1_eps_per_l1_plane
ocs_degree
l0_switches
l1_switches
ocs_coupled_logical_nodes
ocs_full_cross_group_degree
ocs_cross_group_complete
```

## Validation

Smoke test:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_huawei_switch_local_smoke.sh
```

This validates:

- same-tray direct 800G route;
- same-group L0/L1 switch-local forwarding;
- cross-group KSP packet RR;
- cross-group KSP flow hash;
- cross-group SprayPoint packet RR.

OCS dataplane test:

```bash
./HTSIM/tests/functional/run_huawei_ocs_dataplane_tests.sh
```

M=4,N=8 feature test:

```bash
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

This validates:

- OCS graph degree and no intra-group edges;
- SprayPoint state and candidate behavior;
- KSP paths for K=2/4/8/16;
- local direct, same-group, cross-group, packet-RR, and flow-hash cases;
- a 256-rank random all-to-all sample embedded in the 8192-rank topology.

## Reading Results

For EP256 empirical experiments, the most useful files are:

```text
run_config.txt
htsim_<mode>_<choice>_<strategy>.log
output_metrics/flowsInfo.csv
output_metrics/link_load_summary.csv
output_metrics/link_load_by_layer.png
```

Typical interpretation:

- Uniform `huawei_host_l0 up` and `huawei_l0_l1 up` indicate source spray and L0 RR are working.
- `huawei_l1_ocs cross` shows cross-group OCS balance.
- `huawei_host_l0 down` and `huawei_l0_l1 down` can remain skewed because expert hotness creates receiver-side incast.
