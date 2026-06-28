# L2 OCS SprayPoint Routing

SprayPoint is one of the cross-group L2 OCS routing modes in the Huawei topology. It runs on the coupled logical OCS graph described in `02_l2_ocs_design.md`.

Implementation files:

```text
htsim/sim/datacenter/huawei_ocs_spraypoint.h
htsim/sim/datacenter/huawei_ocs_spraypoint.cpp
htsim/sim/datacenter/huawei_ocs_spraypoint_dump.cpp
```

## Routing Scope

SprayPoint is only used when an L1 EPS receives a packet whose destination rank is in another group:

```text
current_group != dst_group
```

The OCS target is the destination group, not a specific destination L1 EPS:

```text
dst_set = all logical_node(dst_group, *, *)
```

Once the packet reaches any logical node in `dst_group`, OCS routing stops and the Huawei L1/L0 downlink FIB delivers the packet to the destination rank.

## Parameters

```text
-huawei_ocs_mode spraypoint
-huawei_ocs_choice packet_rr|flow_hash
-huawei_spray_p <P>
-huawei_spray_h <H>
-huawei_spray_levels auto|<levels>
-huawei_ocs_seed <seed>
```

Meaning:

- `spray_p`: waypoint expansion fanout used while building WP levels.
- `spray_h`: number of parent next-hop candidates kept for forwarding.
- `spray_levels`: number of waypoint levels; `auto` derives it from graph size and degree.
- `huawei_ocs_choice`: how to choose one candidate at runtime.
- `huawei_ocs_seed`: deterministic graph and stable random ordering seed.

Default experiment values:

```text
spray_p = 4
spray_h = 2
spray_levels = auto
huawei_ocs_choice = packet_rr
```

## Destination State

The router builds one destination state per `dst_group`:

```cpp
HuaweiOcsSprayPointDestinationState build_state(uint32_t dst_group) const;
```

For a destination group:

```text
D = all logical nodes in dst_group
WP0 = neighbors(D) - D
WP(level+1) = selected unassigned neighbors of WP(level)
IR = unassigned neighbors of last WP level
OR = all remaining nodes
```

The implementation stores:

```cpp
vector<vector<uint32_t>> waypoint_levels;
vector<uint32_t> inner_ring;
vector<uint32_t> outer_ring;
vector<vector<uint32_t>> parents;
```

## p vs h

`p` is used only to build waypoint coverage:

```text
for each node in WP(level):
    select up to p unassigned neighbors into WP(level+1)
```

`h` is used for forwarding parent candidates:

```text
WP0 -> D                  : up to h parents
WPi -> WP(i-1)            : up to h parents
IR  -> last WP level      : up to h parents
OR  -> shortest path to IR: up to h parents
fallback -> D             : up to h parents
```

So the runtime next-hop fanout is controlled by `h`, not `p`.

## Source-Only Spray

SprayPoint has two runtime phases:

1. Source spray.
2. Destination-specific pointing.

Source spray happens once, when the packet first enters OCS routing from its source group:

```text
source_step = current_group == src_group && !packet.ocs_source_sprayed()
candidates = all OCS neighbors of current logical node
packet.mark_ocs_source_sprayed()
```

If the packet later revisits the source group, it does not spray again. It follows pointing parents.

The one-shot source-spray marker lives in `Packet`:

```cpp
bool ocs_source_sprayed() const;
void mark_ocs_source_sprayed();
void clear_ocs_source_sprayed();
```

## Pointing Phase

After source spray, every non-destination OCS hop uses parents for the packet's destination group:

```text
candidates = parents[dst_group][current_logical_node]
```

Role-specific parent construction:

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

## Candidate Selection

Runtime candidate choice is selected by `-huawei_ocs_choice`:

```text
packet_rr:
  use HuaweiSwitch::next_special_rr()
  switch-local packet round-robin over the candidate set

flow_hash:
  hash(flow_id, pathid, current_node, dst_group, seed)
  stable for a flow/entropy tuple
```

The `packet_rr` implementation is independent of UEC path entropy. It uses a Huawei L1-local counter.

## Current N=8,M=4 Behavior

For the current 8192-rank experiment:

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

The resulting SprayPoint state usually has:

```text
WP0 covers all non-destination logical nodes
IR = 0
OR = 0
```

In this configuration, source spray selects one of 64 OCS neighbors. The next OCS hop is typically already in `WP0`, so pointing uses up to 2 parents into the destination group.

## Data-Plane Integration

The integration point is:

```cpp
HuaweiTopology::l1_special_next_hop(HuaweiSwitch* sw, Packet& pkt)
```

For SprayPoint:

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

The logical next node is mapped back to a physical L1 EPS by preserving the current coupled member:

```cpp
next_l1 = l1_from_logical_node(next_node, current_member)
```

## Inspect SprayPoint State

Build:

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target huawei_ocs_spraypoint_dump -j 8
```

Inspect N=8,M=4:

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

Run the functional test:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```
