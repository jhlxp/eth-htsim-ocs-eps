#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/chen/workplace/infra"
HTSIM_ROOT="${ROOT}/HTSIM"
TESTS_DIR="${HTSIM_ROOT}/tests"
EXPERIMENTS_DIR="${TESTS_DIR}/experiments"
PLOT_TOOLS_DIR="${TESTS_DIR}/plot"
DATASET_DIR="${TESTS_DIR}/data"
DISTRIBUTION_CSV="${DISTRIBUTION_CSV:-${DATASET_DIR}/empirical_pooled_distribution.csv}"
REBUILD_DISTRIBUTION="${REBUILD_DISTRIBUTION:-0}"
LOG_ROOT="${TESTS_DIR}/log"
HTSIM="${HTSIM_ROOT}/htsim/sim/datacenter/htsim_uec"
BUILD_DIR="${HTSIM_ROOT}/htsim/sim/build"

# Huawei 8192-card target family, M=4,N=8.
NODES="${NODES:-8192}"
GROUP_COUNT="${GROUP_COUNT:-16}"
RANKS_PER_GROUP="${RANKS_PER_GROUP:-512}"
RANKS_PER_TRAY="${RANKS_PER_TRAY:-8}"
L1_PLANES="${L1_PLANES:-4}"
L1_EPS_PER_L1_PLANE="${L1_EPS_PER_L1_PLANE:-4}"
OCS_DEGREE="${OCS_DEGREE:-64}"
OCS_SEED="${OCS_SEED:-42}"

# Logical EP256 all-to-all embedded into the 8192-rank fabric.
EP_RANKS="${EP_RANKS:-256}"
RANK_PLACEMENT="${RANK_PLACEMENT:-strided}"
RANK_OFFSET="${RANK_OFFSET:-0}"
PLACEMENT_SEED="${PLACEMENT_SEED:-42}"

# Current single experiment: original empirical single-collective dispatch.
MOE_LAYERS="${MOE_LAYERS:-1}"
INCLUDE_COMBINE="${INCLUDE_COMBINE:-0}"
TOTAL_ASSIGNMENTS_PER_LAYER="${TOTAL_ASSIGNMENTS_PER_LAYER:-0}"
HIDDEN_SIZE="${HIDDEN_SIZE:-7168}"
DTYPE_BYTES="${DTYPE_BYTES:-2}"
TOPK="${TOPK:-8}"
TOKENS_PER_RANK="${TOKENS_PER_RANK:-4096}"
GENERATOR_SEED="${GENERATOR_SEED:-20260624}"

# Routing and transport.
STRAT="${STRAT:-ecmp_rr}"
HUAWEI_OCS_MODE="${HUAWEI_OCS_MODE:-ksp}"
HUAWEI_OCS_CHOICE="${HUAWEI_OCS_CHOICE:-packet_rr}"
KSP_K="${KSP_K:-8}"
KSP_SEED="${KSP_SEED:-42}"
KSP_MAX_HOPS="${KSP_MAX_HOPS:-auto}"
SPRAY_P="${SPRAY_P:-4}"
SPRAY_H="${SPRAY_H:-2}"
SPRAY_LEVELS="${SPRAY_LEVELS:-auto}"

EXT_LINKSPEED_MBPS="${EXT_LINKSPEED_MBPS:-100000}"
LOCAL_LINKSPEED_MBPS="${LOCAL_LINKSPEED_MBPS:-800000}"
LOCAL_LATENCY_NS="${LOCAL_LATENCY_NS:-200}"
OCS_LATENCY_NS="${OCS_LATENCY_NS:-1000}"
MTU_BYTES="${MTU_BYTES:-4150}"
QUEUE_PKTS="${QUEUE_PKTS:-100}"
QUEUE_BYTES=$((QUEUE_PKTS * MTU_BYTES))
END_US="${END_US:-60000000}"
HTSIM_TIMEOUT_SEC="${HTSIM_TIMEOUT_SEC:-0}"
LOG="${LOG:-htsim_${HUAWEI_OCS_MODE}_${HUAWEI_OCS_CHOICE}_${STRAT}.log}"
PLOT="${PLOT:-1}"
LINK_LOAD_SAMPLE="${LINK_LOAD_SAMPLE:-1}"
LINK_LOAD_SAMPLE_US="${LINK_LOAD_SAMPLE_US:-1000}"

OCS_LABEL="${HUAWEI_OCS_MODE}_${HUAWEI_OCS_CHOICE}"
if [[ "${HUAWEI_OCS_MODE}" == "ksp" ]]; then
  OCS_LABEL="${OCS_LABEL}_k${KSP_K}"
elif [[ "${HUAWEI_OCS_MODE}" == "spraypoint" ]]; then
  OCS_LABEL="${OCS_LABEL}_p${SPRAY_P}_h${SPRAY_H}"
fi

RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_huawei_${STRAT}_${OCS_LABEL}_${NODES}nodes_ep${EP_RANKS}_empirical}"
RUN_DIR="${LOG_ROOT}/${RUN_ID}"
DATA_DIR="${RUN_DIR}/data"
PLOTS_DIR="${RUN_DIR}/plots"
TM="${DATA_DIR}/deepseek_r1_ep256_empirical_aggregate.cm"

if [[ "${TOTAL_ASSIGNMENTS_PER_LAYER}" == "auto" ]]; then
  TOTAL_ASSIGNMENTS_PER_LAYER="0"
fi

EFFECTIVE_ASSIGNMENTS_PER_LAYER=$((EP_RANKS * TOKENS_PER_RANK * TOPK))
if [[ "${TOTAL_ASSIGNMENTS_PER_LAYER}" != "0" ]]; then
  EFFECTIVE_ASSIGNMENTS_PER_LAYER="${TOTAL_ASSIGNMENTS_PER_LAYER}"
fi

mkdir -p "${DATA_DIR}" "${PLOTS_DIR}"

COMBINE_ARG="--no-include-combine"
if [[ "${INCLUDE_COMBINE}" == "1" || "${INCLUDE_COMBINE}" == "true" || "${INCLUDE_COMBINE}" == "yes" ]]; then
  COMBINE_ARG="--include-combine"
fi

cat > "${RUN_DIR}/run_config.txt" <<EOF
DeepSeek-R1 empirical EP${EP_RANKS} all-to-all on Huawei ${NODES}-node topology
==============================================================================
run_id: ${RUN_ID}
run_dir: ${RUN_DIR}
created_at: $(date --iso-8601=seconds)

dataset_dir: ${DATASET_DIR}
distribution_csv: ${DISTRIBUTION_CSV}
traffic_matrix: ${TM}
htsim: ${HTSIM}

Huawei topology:
  nodes: ${NODES}
  groups: ${GROUP_COUNT}
  ranks_per_group: ${RANKS_PER_GROUP}
  ranks_per_tray: ${RANKS_PER_TRAY}
  l1_planes: ${L1_PLANES}
  l1_eps_per_l1_plane: ${L1_EPS_PER_L1_PLANE}
  ocs_degree: ${OCS_DEGREE}
  ocs_seed: ${OCS_SEED}

EP traffic:
  ep_ranks: ${EP_RANKS}
  rank_placement: ${RANK_PLACEMENT}
  rank_offset: ${RANK_OFFSET}
  placement_seed: ${PLACEMENT_SEED}
  moe_layers: ${MOE_LAYERS}
  include_combine: ${INCLUDE_COMBINE}
  total_assignments_per_layer: ${TOTAL_ASSIGNMENTS_PER_LAYER}
  effective_assignments_per_layer: ${EFFECTIVE_ASSIGNMENTS_PER_LAYER}
  tokens_per_rank: ${TOKENS_PER_RANK}
  topk: ${TOPK}
  hidden_size: ${HIDDEN_SIZE}
  dtype_bytes: ${DTYPE_BYTES}

Routing:
  switch_strategy: ${STRAT}
  huawei_ocs_mode: ${HUAWEI_OCS_MODE}
  huawei_ocs_choice: ${HUAWEI_OCS_CHOICE}
  ksp_k: ${KSP_K}
  ksp_max_hops: ${KSP_MAX_HOPS}
  spray_p: ${SPRAY_P}
  spray_h: ${SPRAY_H}
  spray_levels: ${SPRAY_LEVELS}

Transport:
  end_us: ${END_US}
  mtu_bytes: ${MTU_BYTES}
  queue_pkts: ${QUEUE_PKTS}
  queue_bytes_actual: ${QUEUE_BYTES}
  external_linkspeed_mbps: ${EXT_LINKSPEED_MBPS}
  local_linkspeed_mbps: ${LOCAL_LINKSPEED_MBPS}
  local_latency_ns: ${LOCAL_LATENCY_NS}
  ocs_latency_ns: ${OCS_LATENCY_NS}
  link_load_sample: ${LINK_LOAD_SAMPLE}
  link_load_sample_us: ${LINK_LOAD_SAMPLE_US}

Outputs:
  htsim_log: ${RUN_DIR}/${LOG}
  logout: ${RUN_DIR}/logout.dat
  metrics: ${RUN_DIR}/output_metrics/flowsInfo.csv
  link_info: ${RUN_DIR}/output_metrics/link_info.csv
  link_load: ${RUN_DIR}/output_metrics/link_load_1ms.csv
  plots: ${PLOTS_DIR}
EOF

echo "[1/6] Build HTSIM UEC"
cmake --build "${BUILD_DIR}" --target htsim_uec -j "${BUILD_JOBS:-8}"

if [[ "${REBUILD_DISTRIBUTION}" == "1" || "${REBUILD_DISTRIBUTION}" == "true" || ! -s "${DISTRIBUTION_CSV}" ]]; then
  echo "[2/6] Build pooled empirical distribution CSV"
  python3 "${DATASET_DIR}/build_empirical_distribution.py" \
    --data-dir "${DATASET_DIR}" \
    --out "${DISTRIBUTION_CSV}"
else
  echo "[2/6] Use existing pooled empirical distribution CSV: ${DISTRIBUTION_CSV}"
fi

echo "[3/6] Generate EP256 empirical all-to-all traffic matrix"
python3 "${EXPERIMENTS_DIR}/generate_deepseek_r1_empirical.py" \
  --empirical-data-dir "${DATASET_DIR}" \
  --empirical-distribution-csv "${DISTRIBUTION_CSV}" \
  --out-dir "${DATA_DIR}" \
  --ranks "${EP_RANKS}" \
  --experts "${EP_RANKS}" \
  --htsim-nodes "${NODES}" \
  --rank-placement "${RANK_PLACEMENT}" \
  --rank-offset "${RANK_OFFSET}" \
  --placement-seed "${PLACEMENT_SEED}" \
  --moe-layers "${MOE_LAYERS}" \
  --topk "${TOPK}" \
  --hidden-size "${HIDDEN_SIZE}" \
  --dtype-bytes "${DTYPE_BYTES}" \
  --tokens-per-rank "${TOKENS_PER_RANK}" \
  --total-assignments-per-layer "${TOTAL_ASSIGNMENTS_PER_LAYER}" \
  --seed "${GENERATOR_SEED}" \
  "${COMBINE_ARG}"

if [[ "${PLOT}" == "1" ]]; then
  echo "[4/6] Plot input traffic distributions"
  python3 "${PLOT_TOOLS_DIR}/plot_deepseek_r1_results.py" \
    --out-dir "${DATA_DIR}" \
    --run-dir "${RUN_DIR}" \
    --plots-dir "${PLOTS_DIR}"
else
  echo "[4/6] Skip input plots"
fi

echo "[5/6] Run HTSIM Huawei traffic matrix"
echo "  tm: ${TM}"
echo "  log: ${RUN_DIR}/${LOG}"
echo "  nodes: ${NODES}, ep_ranks: ${EP_RANKS}, placement: ${RANK_PLACEMENT}"
echo "  mode: ${HUAWEI_OCS_MODE}, choice: ${HUAWEI_OCS_CHOICE}, strat: ${STRAT}"
echo "  effective_assignments_per_layer: ${EFFECTIVE_ASSIGNMENTS_PER_LAYER}"
echo "  queue: ${QUEUE_PKTS} packets ~= ${QUEUE_BYTES} bytes"

if [[ "${LINK_LOAD_SAMPLE}" == "1" || "${LINK_LOAD_SAMPLE}" == "true" || "${LINK_LOAD_SAMPLE}" == "yes" ]]; then
  export HTSIM_LINK_LOAD_SAMPLE=1
  export HTSIM_LINK_LOAD_SAMPLE_US="${LINK_LOAD_SAMPLE_US}"
else
  unset HTSIM_LINK_LOAD_SAMPLE
  unset HTSIM_LINK_LOAD_SAMPLE_US
fi

HTSIM_CMD=(
  "${HTSIM}"
  -tm "${TM}"
  -strat "${STRAT}"
  -load_balancing_algo ecmp
  -linkspeed "${EXT_LINKSPEED_MBPS}"
  -mtu "${MTU_BYTES}"
  -q "${QUEUE_PKTS}"
  -local_tray_size "${RANKS_PER_TRAY}"
  -local_linkspeed "${LOCAL_LINKSPEED_MBPS}"
  -local_latency_ns "${LOCAL_LATENCY_NS}"
  -huawei_ocs_mode "${HUAWEI_OCS_MODE}"
  -huawei_ocs_choice "${HUAWEI_OCS_CHOICE}"
  -huawei_ocs_groups "${GROUP_COUNT}"
  -huawei_ranks_per_group "${RANKS_PER_GROUP}"
  -huawei_ranks_per_tray "${RANKS_PER_TRAY}"
  -huawei_l1_planes "${L1_PLANES}"
  -huawei_l1_eps_per_l1_plane "${L1_EPS_PER_L1_PLANE}"
  -huawei_ocs_degree "${OCS_DEGREE}"
  -huawei_ocs_seed "${OCS_SEED}"
  -huawei_ocs_latency_ns "${OCS_LATENCY_NS}"
  -end "${END_US}"
)

case "${HUAWEI_OCS_MODE}" in
  ksp)
    HTSIM_CMD+=(
      -huawei_ksp_k "${KSP_K}"
      -huawei_ksp_max_hops "${KSP_MAX_HOPS}"
      -huawei_ksp_seed "${KSP_SEED}"
    )
    ;;
  spraypoint)
    HTSIM_CMD+=(
      -huawei_spray_p "${SPRAY_P}"
      -huawei_spray_h "${SPRAY_H}"
      -huawei_spray_levels "${SPRAY_LEVELS}"
    )
    ;;
esac

cd "${RUN_DIR}"
if [[ "${HTSIM_TIMEOUT_SEC}" != "0" ]]; then
  timeout "${HTSIM_TIMEOUT_SEC}" "${HTSIM_CMD[@]}" > "${LOG}" 2>&1
elif [[ -x /usr/bin/time ]]; then
  /usr/bin/time -v "${HTSIM_CMD[@]}" > "${LOG}" 2>&1
else
  "${HTSIM_CMD[@]}" > "${LOG}" 2>&1
fi

echo "Done: ${RUN_DIR}/${LOG}"

if [[ "${PLOT}" == "1" ]]; then
  echo "[6/6] Plot final HTSIM result summaries"
  python3 "${PLOT_TOOLS_DIR}/plot_deepseek_r1_results.py" \
    --out-dir "${DATA_DIR}" \
    --run-dir "${RUN_DIR}" \
    --plots-dir "${PLOTS_DIR}"
else
  echo "[6/6] Skip final plots"
fi
