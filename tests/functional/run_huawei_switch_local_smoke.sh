#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/chen/workplace/infra/HTSIM"
BUILD_DIR="${ROOT}/htsim/sim/build"
DC_DIR="${ROOT}/htsim/sim/datacenter"
LOG_ROOT="${ROOT}/tests/log"
RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_huawei_switch_local_smoke}"
RUN_DIR="${LOG_ROOT}/${RUN_ID}"

NODES="${NODES:-128}"
GROUP_COUNT="${GROUP_COUNT:-4}"
RANKS_PER_GROUP="${RANKS_PER_GROUP:-32}"
RANKS_PER_TRAY="${RANKS_PER_TRAY:-8}"
L1_PLANES="${L1_PLANES:-2}"
L1_EPS_PER_L1_PLANE="${L1_EPS_PER_L1_PLANE:-4}"
OCS_DEGREE="${OCS_DEGREE:-4}"
OCS_SEED="${OCS_SEED:-42}"
FLOW_SIZE_BYTES="${FLOW_SIZE_BYTES:-262144}"
END_US="${END_US:-200000}"
TOPO="${TOPO:-${DC_DIR}/topologies/fat_tree_128_1os.topo}"

mkdir -p "${RUN_DIR}"

cmake --build "${BUILD_DIR}" --target htsim_uec -j "${BUILD_JOBS:-8}"

write_tm() {
  local path="$1"
  local src="$2"
  local dst="$3"
  {
    echo "Nodes ${NODES}"
    echo "Connections 1"
    echo "${src}->${dst} id 1 start 0 size ${FLOW_SIZE_BYTES}"
  } > "${path}"
}

run_case() {
  local name="$1"
  local src="$2"
  local dst="$3"
  local mode="$4"
  local choice="$5"
  local ksp_k="${6:-4}"
  local strat="ecmp_host"
  if [[ "${choice}" == "packet_rr" ]]; then
    strat="ecmp_rr"
  fi

  local case_dir="${RUN_DIR}/${name}"
  mkdir -p "${case_dir}"
  write_tm "${case_dir}/traffic.cm" "${src}" "${dst}"

  echo "[run] ${name}: ${src}->${dst}, mode=${mode}, choice=${choice}, strat=${strat}"
  (
    cd "${case_dir}"
    export HTSIM_LINK_LOAD_SAMPLE=1
    export HTSIM_LINK_LOAD_SAMPLE_US="${LINK_LOAD_SAMPLE_US:-50}"
    "${DC_DIR}/htsim_uec" \
      -topo "${TOPO}" \
      -tm "${case_dir}/traffic.cm" \
      -strat "${strat}" \
      -load_balancing_algo ecmp \
      -linkspeed "${EXT_LINKSPEED_MBPS:-100000}" \
      -mtu "${MTU_BYTES:-4150}" \
      -q "${QUEUE_PKTS:-50}" \
      -local_linkspeed "${LOCAL_LINKSPEED_MBPS:-800000}" \
      -local_latency_ns "${LOCAL_LATENCY_NS:-200}" \
      -huawei_ocs_mode "${mode}" \
      -huawei_ocs_choice "${choice}" \
      -huawei_ocs_groups "${GROUP_COUNT}" \
      -huawei_ranks_per_group "${RANKS_PER_GROUP}" \
      -huawei_ranks_per_tray "${RANKS_PER_TRAY}" \
      -huawei_l1_planes "${L1_PLANES}" \
      -huawei_l1_eps_per_l1_plane "${L1_EPS_PER_L1_PLANE}" \
      -huawei_ocs_degree "${OCS_DEGREE}" \
      -huawei_ocs_seed "${OCS_SEED}" \
      -huawei_ocs_latency_ns "${OCS_LATENCY_NS:-1000}" \
      -huawei_spray_p "${SPRAY_P:-4}" \
      -huawei_spray_h "${SPRAY_H:-2}" \
      -huawei_spray_levels auto \
      -huawei_ksp_k "${ksp_k}" \
      -huawei_ksp_max_hops auto \
      -huawei_ksp_seed "${KSP_SEED:-42}" \
      -end "${END_US}" \
      > htsim.log 2>&1
  )
}

run_case local_direct_800g 0 1 ksp packet_rr 4
run_case same_group_fib_rr 0 8 ksp packet_rr 4
run_case cross_group_ksp_rr 0 32 ksp packet_rr 4
run_case cross_group_ksp_flow 0 32 ksp flow_hash 4
run_case cross_group_spray_rr 0 32 spraypoint packet_rr 4

python3 - "${RUN_DIR}" <<'PY'
import csv
import sys
from collections import defaultdict
from pathlib import Path

run = Path(sys.argv[1])

def read_csv(path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))

def active_links(case):
    case_dir = run / case
    info = {r["link_id"]: r for r in read_csv(case_dir / "output_metrics" / "link_info.csv")}
    totals = defaultdict(int)
    for row in read_csv(case_dir / "output_metrics" / "link_load_1ms.csv"):
        totals[row["link_id"]] += int(float(row["bytes"]))
    return [info[k] for k, v in totals.items() if v > 0]

def flows(case):
    return read_csv(run / case / "output_metrics" / "flowsInfo.csv")

def has_layer(links, layer):
    return any(r.get("layer") == layer for r in links)

def count_layer(links, layer):
    return sum(1 for r in links if r.get("layer") == layer)

def has_local(links):
    return any(r.get("link_name", "").startswith("LOCAL_") for r in links)

checks = {
    "local_direct_800g": lambda l: has_local(l) and not has_layer(l, "huawei_l1_ocs"),
    "same_group_fib_rr": lambda l: has_layer(l, "huawei_host_l0") and has_layer(l, "huawei_l0_l1") and not has_layer(l, "huawei_l1_ocs"),
    "cross_group_ksp_rr": lambda l: has_layer(l, "huawei_l1_ocs"),
    "cross_group_ksp_flow": lambda l: has_layer(l, "huawei_l1_ocs"),
    "cross_group_spray_rr": lambda l: has_layer(l, "huawei_l1_ocs"),
}

summary = ["Huawei switch-local smoke validation OK", ""]
for case, pred in checks.items():
    links = active_links(case)
    fs = flows(case)
    if len(fs) != 1:
        raise AssertionError(f"{case}: expected 1 flow, got {len(fs)}")
    if not pred(links):
        raise AssertionError(f"{case}: active links do not match expected Huawei layers")
    summary.append(
        f"{case}: fct_ns={float(fs[0]['fctNs']):.3f}, active_links={len(links)}, "
        f"local={has_local(links)}, host_l0={count_layer(links, 'huawei_host_l0')}, "
        f"l0_l1={count_layer(links, 'huawei_l0_l1')}, ocs={count_layer(links, 'huawei_l1_ocs')}"
    )

(run / "summary.txt").write_text("\n".join(summary) + "\n")
print(run / "summary.txt")
PY

echo "Done."
echo "  run_dir: ${RUN_DIR}"
echo "  summary: ${RUN_DIR}/summary.txt"
