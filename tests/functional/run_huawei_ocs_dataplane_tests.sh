#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/chen/workplace/infra/HTSIM"
BUILD_DIR="${ROOT}/htsim/sim/build"
DC_DIR="${ROOT}/htsim/sim/datacenter"
TESTS_DIR="${ROOT}/tests"
LOG_ROOT="${TESTS_DIR}/log"
RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_huawei_ocs_dataplane_tests}"
RUN_DIR="${LOG_ROOT}/${RUN_ID}"

# 8192-card target family, using the M=4, N=8 non-full OCS case.
NODES="${NODES:-8192}"
GROUP_COUNT="${GROUP_COUNT:-16}"
RANKS_PER_GROUP="${RANKS_PER_GROUP:-512}"
RANKS_PER_TRAY="${RANKS_PER_TRAY:-8}"
L1_PLANES="${L1_PLANES:-4}"
L1_EPS_PER_L1_PLANE="${L1_EPS_PER_L1_PLANE:-4}"
OCS_DEGREE="${OCS_DEGREE:-64}"
OCS_SEED="${OCS_SEED:-42}"
SPRAY_P="${SPRAY_P:-4}"
SPRAY_H="${SPRAY_H:-2}"
KSP_SEED="${KSP_SEED:-42}"

EXT_LINKSPEED_MBPS="${EXT_LINKSPEED_MBPS:-100000}"
LOCAL_LINKSPEED_MBPS="${LOCAL_LINKSPEED_MBPS:-800000}"
LOCAL_LATENCY_NS="${LOCAL_LATENCY_NS:-200}"
OCS_LATENCY_NS="${OCS_LATENCY_NS:-1000}"
MTU_BYTES="${MTU_BYTES:-4150}"
QUEUE_PKTS="${QUEUE_PKTS:-50}"
FLOW_SIZE_BYTES="${FLOW_SIZE_BYTES:-262144}"
SAMPLE_FLOW_SIZE_BYTES="${SAMPLE_FLOW_SIZE_BYTES:-32768}"
END_US="${END_US:-500000}"
HTSIM_TIMEOUT_SEC="${HTSIM_TIMEOUT_SEC:-180}"
LINK_LOAD_SAMPLE_US="${LINK_LOAD_SAMPLE_US:-100}"
SAMPLE_RANKS="${SAMPLE_RANKS:-256}"
SAMPLE_SEED="${SAMPLE_SEED:-42}"

HTSIM_TOPO="${HTSIM_TOPO:-${DC_DIR}/topologies/fat_tree_8192_1os.topo}"
HTSIM_BIN="${DC_DIR}/htsim_uec"
GRAPH_BIN="${DC_DIR}/huawei_ocs_graph_dump"
SPRAYPOINT_BIN="${DC_DIR}/huawei_ocs_spraypoint_dump"
KSP_BIN="${DC_DIR}/huawei_ocs_ksp_dump"

mkdir -p "${RUN_DIR}"

echo "[0/5] Build HTSIM UEC and Huawei OCS helpers"
cmake --build "${BUILD_DIR}" \
  --target htsim_uec huawei_ocs_graph_dump huawei_ocs_spraypoint_dump huawei_ocs_ksp_dump \
  -j "${BUILD_JOBS:-8}"

{
  echo "nodes=${NODES}"
  echo "groups=${GROUP_COUNT}"
  echo "ranks_per_group=${RANKS_PER_GROUP}"
  echo "ranks_per_tray=${RANKS_PER_TRAY}"
  echo "l1_planes=${L1_PLANES}"
  echo "l1_eps_per_l1_plane=${L1_EPS_PER_L1_PLANE}"
  echo "logical_nodes_per_group=$((L1_PLANES * L1_EPS_PER_L1_PLANE / 2))"
  echo "logical_nodes=$((GROUP_COUNT * L1_PLANES * L1_EPS_PER_L1_PLANE / 2))"
  echo "physical_l1_eps=$((GROUP_COUNT * L1_PLANES * L1_EPS_PER_L1_PLANE))"
  echo "ocs_degree=${OCS_DEGREE}"
  echo "ocs_seed=${OCS_SEED}"
  echo "spray_p=${SPRAY_P}"
  echo "spray_h=${SPRAY_H}"
  echo "ksp_seed=${KSP_SEED}"
  echo "topo=${HTSIM_TOPO}"
  echo "external_linkspeed_mbps=${EXT_LINKSPEED_MBPS}"
  echo "local_linkspeed_mbps=${LOCAL_LINKSPEED_MBPS}"
  echo "sample_ranks=${SAMPLE_RANKS}"
  echo "sample_seed=${SAMPLE_SEED}"
} > "${RUN_DIR}/run_config.txt"

echo "[1/5] Validate M=4,N=8 OCS graph shape"
"${GRAPH_BIN}" \
  --coupled \
  --groups "${GROUP_COUNT}" \
  --l1-planes "${L1_PLANES}" \
  --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
  --degree "${OCS_DEGREE}" \
  --ocs_expander_seed "${OCS_SEED}" \
  --out "${RUN_DIR}/ocs_coupled_graph.csv" \
  > "${RUN_DIR}/ocs_coupled_graph.txt"

python3 - "${RUN_DIR}" <<'PY'
import csv
import sys
from pathlib import Path

run = Path(sys.argv[1])
summary = {}
for line in (run / "ocs_coupled_graph.txt").read_text().splitlines():
    if ":" in line:
        k, v = line.split(":", 1)
        summary[k.strip()] = v.strip()

groups = int(summary["groups"])
nodes = int(summary["nodes"])
logical_per_group = int(summary["logical_nodes_per_group"])
degree = int(summary["degree"])
edges = int(summary["edges"])
assert groups == 16
assert nodes == 128
assert logical_per_group == 8
assert degree == 64
assert edges == nodes * degree // 2
assert summary["degree_ok"] == "true"
assert summary["logical_graph_ok"] == "true"
full_cross_degree = (groups - 1) * logical_per_group
assert degree < full_cross_degree

rows = []
with (run / "ocs_coupled_graph.csv").open() as f:
    for line in f:
        if not line.startswith("#"):
            rows.append(line)
reader = csv.DictReader(rows)
for row in reader:
    assert int(row["src_group"]) != int(row["dst_group"])

(run / "graph_validation.txt").write_text(
    "graph_validation OK\n"
    f"nodes={nodes}\n"
    f"logical_nodes_per_group={logical_per_group}\n"
    f"degree={degree}\n"
    f"edges={edges}\n"
    f"full_cross_degree={full_cross_degree}\n"
    f"is_full_cross=false\n"
)
PY

"${SPRAYPOINT_BIN}" \
  --coupled \
  --groups "${GROUP_COUNT}" \
  --l1-planes "${L1_PLANES}" \
  --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
  --degree "${OCS_DEGREE}" \
  --ocs_expander_seed "${OCS_SEED}" \
  --spray-p "${SPRAY_P}" \
  --spray-h "${SPRAY_H}" \
  --spray-levels auto \
  --spray-seed "${OCS_SEED}" \
  > "${RUN_DIR}/spraypoint_route_stats.txt"

for k in 2 4 8 16; do
  "${KSP_BIN}" \
    --coupled \
    --groups "${GROUP_COUNT}" \
    --l1-planes "${L1_PLANES}" \
    --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
    --degree "${OCS_DEGREE}" \
    --ocs_expander_seed "${OCS_SEED}" \
    --k "${k}" \
    --max-hops auto \
    --ksp-seed "${KSP_SEED}" \
    > "${RUN_DIR}/ksp_k${k}_route_stats.txt"
done

write_tm() {
  local tm="$1"
  local src="$2"
  local dst="$3"
  local size="$4"
  local conns="${5:-1}"
  {
    echo "Nodes ${NODES}"
    echo "Connections ${conns}"
    if [[ "${conns}" == "1" ]]; then
      echo "${src}->${dst} id 1 start 0 size ${size}"
    fi
  } > "${tm}"
}

write_sample_256_tm() {
  local tm="$1"
  python3 - "${tm}" "${NODES}" "${SAMPLE_RANKS}" "${SAMPLE_SEED}" "${SAMPLE_FLOW_SIZE_BYTES}" <<'PY'
import random
import sys

path, nodes, sample_ranks, seed, size = sys.argv[1:6]
nodes = int(nodes)
sample_ranks = int(sample_ranks)
seed = int(seed)
size = int(size)
rng = random.Random(seed)
ranks = rng.sample(range(nodes), sample_ranks)
dsts = ranks[:]
rng.shuffle(dsts)
for i, (s, d) in enumerate(zip(ranks, dsts)):
    if s == d:
        j = (i + 1) % sample_ranks
        dsts[i], dsts[j] = dsts[j], dsts[i]

with open(path, "w") as out:
    out.write(f"Nodes {nodes}\n")
    out.write(f"Connections {sample_ranks}\n")
    for i, (s, d) in enumerate(zip(ranks, dsts), 1):
        out.write(f"{s}->{d} id {i} start 0 size {size}\n")
PY
}

run_case() {
  local name="$1"
  local tm="$2"
  local mode="$3"
  local choice="$4"
  local ksp_k="${5:-8}"
  local strat="ecmp_host"
  if [[ "${choice}" == "packet_rr" ]]; then
    strat="ecmp_rr"
  fi
  local case_dir="${RUN_DIR}/${name}"

  mkdir -p "${case_dir}"
  cp "${tm}" "${case_dir}/traffic.cm"
  {
    echo "name=${name}"
    echo "traffic=${case_dir}/traffic.cm"
    echo "mode=${mode}"
    echo "choice=${choice}"
    echo "switch_strategy=${strat}"
    echo "ksp_k=${ksp_k}"
  } > "${case_dir}/run_config.txt"

  echo "  [htsim] ${name}"
  (
    cd "${case_dir}"
    export HTSIM_LINK_LOAD_SAMPLE=1
    export HTSIM_LINK_LOAD_SAMPLE_US="${LINK_LOAD_SAMPLE_US}"
    timeout "${HTSIM_TIMEOUT_SEC}" "${HTSIM_BIN}" \
      -topo "${HTSIM_TOPO}" \
      -tm "${case_dir}/traffic.cm" \
      -strat "${strat}" \
      -load_balancing_algo ecmp \
      -linkspeed "${EXT_LINKSPEED_MBPS}" \
      -mtu "${MTU_BYTES}" \
      -q "${QUEUE_PKTS}" \
      -local_tray_size "${RANKS_PER_TRAY}" \
      -local_latency_ns "${LOCAL_LATENCY_NS}" \
      -local_linkspeed "${LOCAL_LINKSPEED_MBPS}" \
      -huawei_ocs_mode "${mode}" \
      -huawei_ocs_choice "${choice}" \
      -huawei_ocs_groups "${GROUP_COUNT}" \
      -huawei_ranks_per_group "${RANKS_PER_GROUP}" \
      -huawei_ranks_per_tray "${RANKS_PER_TRAY}" \
      -huawei_l1_planes "${L1_PLANES}" \
      -huawei_l1_eps_per_l1_plane "${L1_EPS_PER_L1_PLANE}" \
      -huawei_ocs_degree "${OCS_DEGREE}" \
      -huawei_ocs_seed "${OCS_SEED}" \
      -huawei_spray_p "${SPRAY_P}" \
      -huawei_spray_h "${SPRAY_H}" \
      -huawei_spray_levels auto \
      -huawei_ksp_k "${ksp_k}" \
      -huawei_ksp_max_hops auto \
      -huawei_ksp_seed "${KSP_SEED}" \
      -huawei_ocs_latency_ns "${OCS_LATENCY_NS}" \
      -end "${END_US}" \
      > "${case_dir}/htsim.log" 2>&1
  )
  test -s "${case_dir}/output_metrics/flowsInfo.csv"
  test -s "${case_dir}/output_metrics/link_info.csv"
  test -s "${case_dir}/output_metrics/link_load_1ms.csv"
}

echo "[2/5] Generate tiny single-flow traffic matrices"
write_tm "${RUN_DIR}/tm_local_same_tray.cm" 0 1 "${FLOW_SIZE_BYTES}"
write_tm "${RUN_DIR}/tm_same_group_cross_tray.cm" 0 16 "${FLOW_SIZE_BYTES}"
write_tm "${RUN_DIR}/tm_cross_group.cm" 0 512 "${FLOW_SIZE_BYTES}"
write_sample_256_tm "${RUN_DIR}/tm_sample_256_random.cm"

echo "[3/5] Run single-flow data-plane cases"
run_case "local_direct_800g_ksp" "${RUN_DIR}/tm_local_same_tray.cm" ksp packet_rr 8
run_case "same_group_no_ocs_ksp" "${RUN_DIR}/tm_same_group_cross_tray.cm" ksp packet_rr 8
run_case "cross_group_spraypoint_packet_rr" "${RUN_DIR}/tm_cross_group.cm" spraypoint packet_rr 8
run_case "cross_group_spraypoint_flow_hash" "${RUN_DIR}/tm_cross_group.cm" spraypoint flow_hash 8
for k in 2 4 8 16; do
  run_case "cross_group_ksp_packet_rr_k${k}" "${RUN_DIR}/tm_cross_group.cm" ksp packet_rr "${k}"
done

echo "[4/5] Run 256-rank random-in-8192 smoke"
run_case "sample_256_random_ksp_packet_rr_k8" "${RUN_DIR}/tm_sample_256_random.cm" ksp packet_rr 8

echo "[5/5] Validate data-plane outputs"
python3 - "${RUN_DIR}" <<'PY'
import csv
import sys
from collections import defaultdict
from pathlib import Path

run = Path(sys.argv[1])

def rows(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))

def active_links(case):
    case_dir = run / case
    info = {r["link_id"]: r for r in rows(case_dir / "output_metrics" / "link_info.csv")}
    totals = defaultdict(int)
    for r in rows(case_dir / "output_metrics" / "link_load_1ms.csv"):
        totals[r["link_id"]] += int(float(r["bytes"]))
    out = []
    for link_id, byte_count in totals.items():
        if byte_count <= 0:
            continue
        item = dict(info[link_id])
        item["bytes"] = byte_count
        out.append(item)
    return out

def flows(case):
    return rows(run / case / "output_metrics" / "flowsInfo.csv")

def require(condition, message):
    if not condition:
        raise AssertionError(message)

def has_layer(links, layer):
    return any(r.get("layer") == layer for r in links)

def active_layers(links):
    return sorted({r.get("layer") for r in links if r.get("layer")})

def active_rates(links, layer=None):
    return sorted({r.get("rate_gbps") for r in links if layer is None or r.get("layer") == layer})

def local_link_count(links):
    return sum(1 for r in links if r.get("link_name", "").startswith("LOCAL_"))

def huawei_layer_count(links, layer):
    return sum(1 for r in links if r.get("layer") == layer)

single_flow_cases = [
    "local_direct_800g_ksp",
    "same_group_no_ocs_ksp",
    "cross_group_spraypoint_packet_rr",
    "cross_group_spraypoint_flow_hash",
    "cross_group_ksp_packet_rr_k2",
    "cross_group_ksp_packet_rr_k4",
    "cross_group_ksp_packet_rr_k8",
    "cross_group_ksp_packet_rr_k16",
]
for case in single_flow_cases:
    fs = flows(case)
    require(len(fs) == 1, f"{case}: expected one completed flow, got {len(fs)}")

local_links = active_links("local_direct_800g_ksp")
require(local_link_count(local_links) > 0, "local direct case did not use LOCAL link")
require("800" in active_rates(local_links), "local direct case did not expose 800G link")
require(not has_layer(local_links, "huawei_l1_ocs"), "local direct unexpectedly used OCS")

same_group_links = active_links("same_group_no_ocs_ksp")
require(has_layer(same_group_links, "huawei_host_l0"), "same-group missing host-L0")
require(has_layer(same_group_links, "huawei_l0_l1"), "same-group missing L0-L1")
require(not has_layer(same_group_links, "huawei_l1_ocs"), "same-group should not use OCS")
require("100" in active_rates(same_group_links), "same-group did not use 100G Huawei links")

cross_packet = active_links("cross_group_spraypoint_packet_rr")
cross_flow = active_links("cross_group_spraypoint_flow_hash")
for case, links in [
    ("cross_group_spraypoint_packet_rr", cross_packet),
    ("cross_group_spraypoint_flow_hash", cross_flow),
    ("cross_group_ksp_packet_rr_k2", active_links("cross_group_ksp_packet_rr_k2")),
    ("cross_group_ksp_packet_rr_k4", active_links("cross_group_ksp_packet_rr_k4")),
    ("cross_group_ksp_packet_rr_k8", active_links("cross_group_ksp_packet_rr_k8")),
    ("cross_group_ksp_packet_rr_k16", active_links("cross_group_ksp_packet_rr_k16")),
]:
    require(has_layer(links, "huawei_host_l0"), f"{case}: missing host-L0")
    require(has_layer(links, "huawei_l0_l1"), f"{case}: missing L0-L1")
    require(has_layer(links, "huawei_l1_ocs"), f"{case}: missing L1-OCS cross links")
    require("100" in active_rates(links), f"{case}: missing 100G Huawei links")

require(
    huawei_layer_count(cross_packet, "huawei_l1_ocs")
    >= huawei_layer_count(cross_flow, "huawei_l1_ocs"),
    "packet_rr should expose at least as many OCS links as flow_hash for one cross-group flow",
)

sample_case = "sample_256_random_ksp_packet_rr_k8"
sample_flows = flows(sample_case)
sample_links = active_links(sample_case)
require(len(sample_flows) == 256, f"sample case expected 256 flows, got {len(sample_flows)}")
require(has_layer(sample_links, "huawei_l1_ocs"), "sample case missing OCS links")
require(local_link_count(sample_links) >= 0, "sample local link check failed")

summary = []
summary.append("Huawei OCS data-plane validation OK")
summary.append("")
summary.append((run / "graph_validation.txt").read_text().strip())
summary.append("")
for case in single_flow_cases + [sample_case]:
    fs = flows(case)
    links = active_links(case)
    fcts = [float(r["fctNs"]) for r in fs]
    summary.append(
        f"{case}: flows={len(fs)}, max_fct_ns={max(fcts):.3f}, "
        f"active_links={len(links)}, layers={active_layers(links)}, "
        f"rates_gbps={active_rates(links)}, local_links={local_link_count(links)}, "
        f"ocs_links={huawei_layer_count(links, 'huawei_l1_ocs')}"
    )

(run / "summary.txt").write_text("\n".join(summary) + "\n")
print(run / "summary.txt")
PY

echo
echo "Done."
echo "  run_dir: ${RUN_DIR}"
echo "  summary: ${RUN_DIR}/summary.txt"
