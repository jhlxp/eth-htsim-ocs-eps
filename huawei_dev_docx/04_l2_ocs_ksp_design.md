# L2 OCS KSP Routing

KSP is the complete-path routing mode for cross-group Huawei L2 OCS traffic. It runs on the same coupled logical OCS graph described in `02_l2_ocs_design.md`.

Implementation files:

```text
htsim/sim/datacenter/huawei_ocs_ksp.h
htsim/sim/datacenter/huawei_ocs_ksp.cpp
htsim/sim/datacenter/huawei_ocs_ksp_dump.cpp
```

## Routing Scope

KSP is used only when an L1 EPS sees cross-group traffic:

```text
current_group != dst_group
```

The path target is the destination group:

```text
dst_set = all logical_node(dst_group, *, *)
```

Any logical node in `dst_group` can terminate the OCS segment. Group-local L1/L0 FIB entries then deliver the packet to the destination rank.

## Parameters

```text
-huawei_ocs_mode ksp
-huawei_ocs_choice packet_rr|flow_hash
-huawei_ksp_k <K>
-huawei_ksp_max_hops auto|<hops>
-huawei_ksp_seed <seed>
-huawei_ksp_max_paths_per_pair <limit>
```

Meaning:

- `huawei_ksp_k`: number of candidate paths per `(src_logical_node, dst_group)`.
- `huawei_ksp_max_hops`: maximum OCS hops; `auto` means no explicit small cap.
- `huawei_ksp_seed`: deterministic tie-break seed.
- `huawei_ksp_max_paths_per_pair`: guardrail for path generation.
- `huawei_ocs_choice`: runtime path selection policy.

Default experiment values:

```text
ksp_k = 8
ksp_max_hops = auto
ksp_seed = 42
huawei_ocs_choice = packet_rr
```

## Path Table

The router precomputes:

```text
paths[src_logical_node][dst_group] = [
  [src_logical_node, ..., dst_group_logical_node],
  ...
]
```

Each stored path:

- starts at the source logical node;
- ends at any logical node in `dst_group`;
- is simple;
- is sorted by hop count with deterministic tie-breaking.

The source class is:

```cpp
class HuaweiOcsKspRouter
```

Main APIs:

```cpp
const vector<HuaweiOcsKspPath>& paths(uint32_t src_node, uint32_t dst_group) const;
uint32_t choose_path(...);
uint32_t next_hop(src_node, dst_group, path_id, current_node) const;
```

## Path Generation

The implementation uses a Yen-style K shortest simple path search over a multi-target destination set.

High-level flow:

```text
1. Find the first BFS shortest path from src_node to any node in dst_group.
2. Add it to accepted paths.
3. Generate spur candidates by deviating from accepted paths.
4. Ban edges that recreate an already accepted prefix.
5. Ban root-prefix nodes to keep paths simple.
6. Add the best candidate by length and deterministic tie-break.
7. Stop at K paths or when no candidates remain.
```

This avoids enumerating all possible paths in the expander graph.

## Packet Metadata

KSP needs packet metadata because the source selects a complete path and intermediate L1 EPS must continue along that exact path.

The packet stores:

```cpp
bool has_ocs_ksp_route() const;
void set_ocs_ksp_route(uint32_t src_node, uint32_t dst_group, uint32_t path_id);
void clear_ocs_ksp_route();

uint32_t ocs_ksp_src_node() const;
uint32_t ocs_ksp_dst_group() const;
uint32_t ocs_ksp_path_id() const;
```

The metadata is cleared when a packet is reinitialized and when the packet reaches the destination group.

## Runtime Forwarding

Source L1 EPS:

```text
src_node = current logical node
dst_group = rank_group(pkt.dst)
path_id = choose_path(src_node, dst_group, pkt)
pkt.set_ocs_ksp_route(src_node, dst_group, path_id)
next = next_hop(src_node, dst_group, path_id, current_node)
```

Middle L1 EPS:

```text
src_node = pkt.ocs_ksp_src_node()
dst_group = pkt.ocs_ksp_dst_group()
path_id = pkt.ocs_ksp_path_id()
current_node = current logical node
next = next_hop(src_node, dst_group, path_id, current_node)
```

Destination group:

```text
pkt.clear_ocs_ksp_route()
return nullptr from OCS resolver
HuaweiSwitch continues with normal downlink FIB
```

## Path Selection

`-huawei_ocs_choice` controls how one of the K candidate paths is selected:

```text
packet_rr:
  source L1 switch-local round-robin over K paths

flow_hash:
  stable hash over flow_id/pathid/src/dst_group
```

`packet_rr` is useful for stress-testing packet-level path spreading. `flow_hash` is closer to conventional ECMP/KSP flow placement.

## Data-Plane Integration

The integration point is:

```cpp
HuaweiTopology::l1_special_next_hop(HuaweiSwitch* sw, Packet& pkt)
```

For KSP:

```cpp
if (!pkt.has_ocs_ksp_route()) {
    path_id = _ksp->choose_path(...);
    pkt.set_ocs_ksp_route(src_node, dst_group, path_id);
}

next_node = _ksp->next_hop(src_node, dst_group, path_id, current_node);
```

The next logical node is mapped to a physical L1 EPS by preserving the coupled member:

```cpp
next_l1 = l1_from_logical_node(next_node, current_member)
```

## Inspect KSP Paths

Build:

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target huawei_ocs_ksp_dump -j 8
```

Inspect N=8,M=4 with K=8:

```bash
./htsim/sim/datacenter/huawei_ocs_ksp_dump \
  --coupled \
  --groups 16 \
  --l1-planes 4 \
  --l1-eps-per-l1-plane 4 \
  --degree 64 \
  --ocs_expander_seed 42 \
  --k 8 \
  --max-hops auto \
  --ksp-seed 42
```

Run the functional test:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

Run the EP256 experiment with KSP:

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=ksp HUAWEI_OCS_CHOICE=packet_rr KSP_K=8 \
  ./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```
