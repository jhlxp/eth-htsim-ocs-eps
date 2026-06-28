#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/chen/workplace/infra/HTSIM"
BUILD_DIR="${ROOT}/htsim/sim/build"
DC_DIR="${ROOT}/htsim/sim/datacenter"
TESTS_DIR="${ROOT}/tests"
LOG_ROOT="${TESTS_DIR}/log"
RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)_ocs_m4n8_feature_tests}"
RUN_DIR="${LOG_ROOT}/${RUN_ID}"

M="${M:-4}"
N="${N:-8}"
OCS_GROUPS="${OCS_GROUPS:-16}"
L1_PLANES="${L1_PLANES:-4}"
L1_EPS_PER_L1_PLANE="${L1_EPS_PER_L1_PLANE:-4}"
OCS_DEGREE="${OCS_DEGREE:-64}"
OCS_SEED="${OCS_SEED:-42}"
SPRAY_P="${SPRAY_P:-4}"
SPRAY_H="${SPRAY_H:-2}"
KSP_SEED="${KSP_SEED:-42}"
SAMPLE_SEED="${SAMPLE_SEED:-42}"
SAMPLE_RANKS="${SAMPLE_RANKS:-256}"

RUN_HTSIM_SMOKE="${RUN_HTSIM_SMOKE:-1}"
RUN_SAMPLE_256_HTSIM="${RUN_SAMPLE_256_HTSIM:-1}"
HTSIM_TIMEOUT_SEC="${HTSIM_TIMEOUT_SEC:-180}"
HTSIM_FLOW_SIZE_BYTES="${HTSIM_FLOW_SIZE_BYTES:-262144}"
HTSIM_SAMPLE_FLOW_SIZE_BYTES="${HTSIM_SAMPLE_FLOW_SIZE_BYTES:-65536}"
HTSIM_END_US="${HTSIM_END_US:-500000}"
HTSIM_TOPO="${HTSIM_TOPO:-${DC_DIR}/topologies/fat_tree_8192_1os.topo}"

GRAPH_BIN="${DC_DIR}/huawei_ocs_graph_dump"
SPRAY_BIN="${DC_DIR}/huawei_ocs_spraypoint_dump"
KSP_BIN="${DC_DIR}/huawei_ocs_ksp_dump"
HTSIM_BIN="${DC_DIR}/htsim_uec"

mkdir -p "${RUN_DIR}"

echo "[0/7] Build dump tools and HTSIM UEC"
cmake --build "${BUILD_DIR}" \
  --target huawei_ocs_graph_dump huawei_ocs_spraypoint_dump huawei_ocs_ksp_dump htsim_uec \
  -j "${BUILD_JOBS:-8}"

{
  echo "M=${M}"
  echo "N=${N}"
  echo "groups=${OCS_GROUPS}"
  echo "l1_planes=${L1_PLANES}"
  echo "l1_eps_per_l1_plane=${L1_EPS_PER_L1_PLANE}"
  echo "logical_nodes_per_group=$((L1_PLANES * L1_EPS_PER_L1_PLANE / 2))"
  echo "logical_nodes=$((OCS_GROUPS * L1_PLANES * L1_EPS_PER_L1_PLANE / 2))"
  echo "physical_l1_eps=$((OCS_GROUPS * L1_PLANES * L1_EPS_PER_L1_PLANE))"
  echo "ocs_degree=${OCS_DEGREE}"
  echo "ocs_seed=${OCS_SEED}"
  echo "spray_p=${SPRAY_P}"
  echo "spray_h=${SPRAY_H}"
  echo "ksp_seed=${KSP_SEED}"
  echo "sample_seed=${SAMPLE_SEED}"
  echo "sample_ranks=${SAMPLE_RANKS}"
  echo "run_htsim_smoke=${RUN_HTSIM_SMOKE}"
  echo "run_sample_256_htsim=${RUN_SAMPLE_256_HTSIM}"
} > "${RUN_DIR}/run_config.txt"

GRAPH_CSV="${RUN_DIR}/ocs_m4n8_coupled_graph.csv"
GRAPH_TXT="${RUN_DIR}/ocs_m4n8_coupled_graph.txt"

echo "[1/7] Build and validate M=${M}, N=${N} coupled OCS graph"
"${GRAPH_BIN}" \
  --coupled \
  --groups "${OCS_GROUPS}" \
  --l1-planes "${L1_PLANES}" \
  --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
  --degree "${OCS_DEGREE}" \
  --ocs_expander_seed "${OCS_SEED}" \
  --out "${GRAPH_CSV}" \
  | tee "${GRAPH_TXT}"

python3 - "${GRAPH_TXT}" "${GRAPH_CSV}" "${RUN_DIR}/graph_validation.txt" <<'PY'
import csv
import sys
from collections import Counter, defaultdict

txt_path, csv_path, out_path = sys.argv[1:4]
summary = {}
for line in open(txt_path):
    if ":" in line:
        k, v = line.strip().split(":", 1)
        summary[k.strip()] = v.strip()

groups = int(summary["groups"])
l1_planes = int(summary["l1_planes"])
l1_eps = int(summary["l1_eps_per_l1_plane"])
logical_per_group = int(summary["logical_nodes_per_group"])
nodes = int(summary["nodes"])
degree = int(summary["degree"])
edges = int(summary["edges"])

assert groups == 16, groups
assert l1_planes == 4, l1_planes
assert l1_eps == 4, l1_eps
assert logical_per_group == 8, logical_per_group
assert nodes == 128, nodes
assert degree == 64, degree
assert edges == nodes * degree // 2, edges
assert summary["degree_ok"] == "true"
assert summary["logical_graph_ok"] == "true"

rows = []
with open(csv_path, newline="") as f:
    for line in f:
        if not line.startswith("#"):
            rows.append(line)
reader = csv.DictReader(rows)
degree_count = Counter()
round_edges = defaultdict(list)
seen_edges = set()
for row in reader:
    src = int(row["src_node"])
    dst = int(row["dst_node"])
    ocs = int(row["ocs"])
    sg = int(row["src_group"])
    dg = int(row["dst_group"])
    sp = int(row["src_l1_plane"])
    dp = int(row["dst_l1_plane"])
    scp = int(row["src_coupled_pair"])
    dcp = int(row["dst_coupled_pair"])
    assert 0 <= src < nodes
    assert 0 <= dst < nodes
    assert sg != dg
    assert src == sg * logical_per_group + sp * (l1_eps // 2) + scp
    assert dst == dg * logical_per_group + dp * (l1_eps // 2) + dcp
    e = tuple(sorted((src, dst)))
    assert e not in seen_edges
    seen_edges.add(e)
    degree_count[src] += 1
    degree_count[dst] += 1
    round_edges[ocs].append((src, dst))

assert len(seen_edges) == edges
assert len(round_edges) == degree
for node in range(nodes):
    assert degree_count[node] == degree, (node, degree_count[node])
for ocs, pairs in round_edges.items():
    assert len(pairs) == nodes // 2, (ocs, len(pairs))
    covered = Counter()
    for a, b in pairs:
        covered[a] += 1
        covered[b] += 1
    assert len(covered) == nodes, (ocs, len(covered))
    assert all(v == 1 for v in covered.values()), ocs

full_cross_degree = (groups - 1) * logical_per_group
full_cross_edges = nodes * full_cross_degree // 2
with open(out_path, "w") as out:
    out.write("M=4,N=8 coupled OCS graph validation OK\n")
    out.write(f"nodes={nodes}\n")
    out.write(f"logical_nodes_per_group={logical_per_group}\n")
    out.write(f"degree={degree}\n")
    out.write(f"edges={edges}\n")
    out.write(f"full_cross_degree={full_cross_degree}\n")
    out.write(f"full_cross_edges={full_cross_edges}\n")
    out.write(f"is_full_cross_graph={edges == full_cross_edges}\n")
    out.write(f"ocs_rounds={len(round_edges)}\n")
    out.write(f"edges_per_round={nodes // 2}\n")
print(out_path)
PY

echo "[2/7] SprayPoint packet-level and flow-level query checks"
for mode in packet_rr flow_hash; do
  for rr in 0 1; do
    out="${RUN_DIR}/spraypoint_${mode}_rr${rr}.txt"
    "${SPRAY_BIN}" \
      --coupled \
      --groups "${OCS_GROUPS}" \
      --l1-planes "${L1_PLANES}" \
      --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
      --degree "${OCS_DEGREE}" \
      --ocs_expander_seed "${OCS_SEED}" \
      --spray-p "${SPRAY_P}" \
      --spray-h "${SPRAY_H}" \
      --spray-levels auto \
      --spray-seed "${OCS_SEED}" \
      --choice "${mode}" \
      --query-flow-id 12345 \
      --query-path-id 7 \
      --query-rr-counter "${rr}" \
      --query-src-group 0 \
      --query-src-l1-plane 0 \
      --query-src-l1-eps 0 \
      --query-dst-group 1 \
      --out "${RUN_DIR}/spraypoint_${mode}_rr${rr}.csv" \
      > "${out}"
  done
done

python3 - "${RUN_DIR}" <<'PY'
import re
import sys
from pathlib import Path

run = Path(sys.argv[1])

def read(name):
    text = (run / name).read_text()
    assert "logical_graph_ok: true" in text
    return text

def pick(text, key):
    m = re.search(rf"^{re.escape(key)}: (\S+)$", text, re.M)
    assert m, key
    return m.group(1)

rr0 = read("spraypoint_packet_rr_rr0.txt")
rr1 = read("spraypoint_packet_rr_rr1.txt")
fh0 = read("spraypoint_flow_hash_rr0.txt")
fh1 = read("spraypoint_flow_hash_rr1.txt")

assert pick(rr0, "choose_source_packet_rr") != pick(rr1, "choose_source_packet_rr")
assert pick(rr0, "choose_pointing_packet_rr") != pick(rr1, "choose_pointing_packet_rr")
assert pick(fh0, "choose_source_flow_hash") == pick(fh1, "choose_source_flow_hash")
assert pick(fh0, "choose_pointing_flow_hash") == pick(fh1, "choose_pointing_flow_hash")

out = run / "spraypoint_validation.txt"
out.write_text(
    "SprayPoint validation OK\n"
    f"packet_rr source rr0/rr1={pick(rr0, 'choose_source_packet_rr')}/{pick(rr1, 'choose_source_packet_rr')}\n"
    f"packet_rr pointing rr0/rr1={pick(rr0, 'choose_pointing_packet_rr')}/{pick(rr1, 'choose_pointing_packet_rr')}\n"
    f"flow_hash source={pick(fh0, 'choose_source_flow_hash')}\n"
    f"flow_hash pointing={pick(fh0, 'choose_pointing_flow_hash')}\n"
)
print(out)
PY

echo "[3/7] KSP k=2/4/8/16 path checks"
for k in 2 4 8 16; do
  "${KSP_BIN}" \
    --coupled \
    --groups "${OCS_GROUPS}" \
    --l1-planes "${L1_PLANES}" \
    --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
    --degree "${OCS_DEGREE}" \
    --ocs_expander_seed "${OCS_SEED}" \
    --k "${k}" \
    --max-hops auto \
    --ksp-seed "${KSP_SEED}" \
    --choice packet_rr \
    --query-rr-counter "$((k - 1))" \
    --query-src-group 0 \
    --query-src-l1-plane 0 \
    --query-src-l1-eps 0 \
    --query-dst-group 1 \
    --out "${RUN_DIR}/ksp_k${k}_paths.csv" \
    > "${RUN_DIR}/ksp_k${k}_query.txt"
done

"${KSP_BIN}" \
  --coupled \
  --groups "${OCS_GROUPS}" \
  --l1-planes "${L1_PLANES}" \
  --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
  --degree "${OCS_DEGREE}" \
  --ocs_expander_seed "${OCS_SEED}" \
  --k 4 \
  --max-hops auto \
  --ksp-seed "${KSP_SEED}" \
  --choice flow_hash \
  --query-flow-id 12345 \
  --query-packet-id 0 \
  --query-src-group 0 \
  --query-src-l1-plane 0 \
  --query-src-l1-eps 0 \
  --query-dst-group 1 \
  > "${RUN_DIR}/ksp_k4_flow_hash_packet0.txt"

"${KSP_BIN}" \
  --coupled \
  --groups "${OCS_GROUPS}" \
  --l1-planes "${L1_PLANES}" \
  --l1-eps-per-l1-plane "${L1_EPS_PER_L1_PLANE}" \
  --degree "${OCS_DEGREE}" \
  --ocs_expander_seed "${OCS_SEED}" \
  --k 4 \
  --max-hops auto \
  --ksp-seed "${KSP_SEED}" \
  --choice flow_hash \
  --query-flow-id 12345 \
  --query-packet-id 9 \
  --query-src-group 0 \
  --query-src-l1-plane 0 \
  --query-src-l1-eps 0 \
  --query-dst-group 1 \
  > "${RUN_DIR}/ksp_k4_flow_hash_packet9.txt"

python3 - "${RUN_DIR}" "${GRAPH_CSV}" <<'PY'
import csv
import re
import sys
from pathlib import Path

run = Path(sys.argv[1])
graph_csv = Path(sys.argv[2])
groups = 16
logical_per_group = 8

edges = set()
with graph_csv.open(newline="") as f:
    for line in f:
        if line.startswith("#"):
            continue
        rows = [line] + list(f)
        break
reader = csv.DictReader(rows)
for row in reader:
    a = int(row["src_node"])
    b = int(row["dst_node"])
    edges.add((a, b))
    edges.add((b, a))

def read_csv_skip_comments(path):
    lines = [line for line in path.read_text().splitlines(True) if not line.startswith("#")]
    return list(csv.DictReader(lines))

def chosen(path):
    m = re.search(r"^query chosen_path_id: (\d+)$", path.read_text(), re.M)
    assert m, path
    return int(m.group(1))

for k in (2, 4, 8, 16):
    text = (run / f"ksp_k{k}_query.txt").read_text()
    assert "logical_graph_ok: true" in text
    assert f"k: {k}" in text
    assert f"query path_count: {k}" in text
    assert chosen(run / f"ksp_k{k}_query.txt") == k - 1

    rows = read_csv_skip_comments(run / f"ksp_k{k}_paths.csv")
    assert rows, k
    counts = {}
    for row in rows:
        src = int(row["src_node"])
        src_group = int(row["src_group"])
        dst_group = int(row["dst_group"])
        path_id = int(row["path_id"])
        nodes = [int(x) for x in row["path_nodes"].split(";") if x]
        assert nodes[0] == src
        assert nodes[-1] // logical_per_group == dst_group
        assert src_group != dst_group
        for a, b in zip(nodes, nodes[1:]):
            assert (a, b) in edges, (k, a, b)
        counts[(src, dst_group)] = max(counts.get((src, dst_group), 0), path_id + 1)
    assert min(counts.values()) == k, (k, min(counts.values()))

fh0 = chosen(run / "ksp_k4_flow_hash_packet0.txt")
fh9 = chosen(run / "ksp_k4_flow_hash_packet9.txt")
assert fh0 == fh9

out = run / "ksp_validation.txt"
out.write_text(
    "KSP validation OK\n"
    "validated_k=2,4,8,16\n"
    f"k4_flow_hash_chosen_path={fh0}\n"
)
print(out)
PY

echo "[4/7] Generate 256 random ranks embedded in the 8192-card cluster"
python3 - "${RUN_DIR}" "${SAMPLE_SEED}" "${SAMPLE_RANKS}" <<'PY'
import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

run = Path(sys.argv[1])
seed = int(sys.argv[2])
sample_count = int(sys.argv[3])
rng = random.Random(seed)
ranks = sorted(rng.sample(range(8192), sample_count))

def meta(rank):
    group_size = 512
    group = rank // group_size
    in_group = rank % group_size
    l0 = in_group // 64
    in_l0 = in_group % 64
    tray_in_l0 = in_l0 // 8
    local_rank = in_l0 % 8
    global_tray = rank // 8
    return group, l0, tray_in_l0, global_tray, local_rank

sample_csv = run / f"sample_{sample_count}_ranks_seed{seed}.csv"
with sample_csv.open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["sample_index", "rank", "group", "l0", "tray_in_l0", "global_tray", "local_rank"])
    for i, rank in enumerate(ranks):
        writer.writerow([i, rank, *meta(rank)])

by_tray = defaultdict(list)
by_group = defaultdict(list)
for rank in ranks:
    group, l0, tray, global_tray, local_rank = meta(rank)
    by_tray[global_tray].append(rank)
    by_group[group].append(rank)

local_pair = next((v[:2] for v in by_tray.values() if len(v) >= 2), None)
same_group_pair = None
for group, values in by_group.items():
    values = sorted(values)
    for a in values:
        for b in values:
            if a < b and a // 8 != b // 8:
                same_group_pair = [a, b]
                break
        if same_group_pair:
            break
    if same_group_pair:
        break
cross_pair = None
for a in ranks:
    for b in ranks:
        if a != b and meta(a)[0] != meta(b)[0]:
            cross_pair = [a, b]
            break
    if cross_pair:
        break

assert local_pair, "random sample did not contain a same-tray pair"
assert same_group_pair, "random sample did not contain a same-group cross-tray pair"
assert cross_pair, "random sample did not contain a cross-group pair"

perm = ranks[:]
rng.shuffle(perm)
pairs = list(zip(perm[:sample_count // 2], perm[sample_count // 2:]))
tm = run / "sample_256_permutation.cm"
with tm.open("w") as f:
    f.write("Nodes 8192\n")
    f.write(f"Connections {len(pairs)}\n")
    for i, (src, dst) in enumerate(pairs, 1):
        f.write(f"{src}->{dst} id {i} start 0 size 65536\n")

env = run / "selected_sample_pairs.env"
env.write_text(
    f"LOCAL_SRC={local_pair[0]}\n"
    f"LOCAL_DST={local_pair[1]}\n"
    f"SAME_GROUP_SRC={same_group_pair[0]}\n"
    f"SAME_GROUP_DST={same_group_pair[1]}\n"
    f"CROSS_SRC={cross_pair[0]}\n"
    f"CROSS_DST={cross_pair[1]}\n"
)

groups = Counter(meta(rank)[0] for rank in ranks)
trays = Counter(meta(rank)[3] for rank in ranks)
summary = run / "sample_256_summary.txt"
summary.write_text(
    "256-rank sample embedded in 8192-card cluster OK\n"
    f"seed={seed}\n"
    f"sample_count={sample_count}\n"
    f"groups_covered={len(groups)}\n"
    f"local_trays_with_two_or_more={sum(1 for c in trays.values() if c >= 2)}\n"
    f"local_pair={local_pair[0]}->{local_pair[1]}\n"
    f"same_group_cross_tray_pair={same_group_pair[0]}->{same_group_pair[1]}\n"
    f"cross_group_pair={cross_pair[0]}->{cross_pair[1]}\n"
    f"permutation_flows={len(pairs)}\n"
)
print(sample_csv)
print(summary)
print(env)
PY

source "${RUN_DIR}/selected_sample_pairs.env"

echo "[5/7] Validate KSP routes for the 256 sampled ranks"
python3 - "${RUN_DIR}" "${RUN_DIR}/ksp_k16_paths.csv" "${RUN_DIR}/sample_256_permutation.cm" <<'PY'
import csv
import re
import sys
from pathlib import Path

run = Path(sys.argv[1])
ksp_csv = Path(sys.argv[2])
tm_path = Path(sys.argv[3])
logical_per_group = 8

def rank_group(rank):
    return rank // 512

traffic_pairs = []
for line in tm_path.read_text().splitlines():
    m = re.match(r"^(\d+)->(\d+) id ", line)
    if m:
        traffic_pairs.append((int(m.group(1)), int(m.group(2))))
assert traffic_pairs, tm_path

lines = [line for line in ksp_csv.read_text().splitlines(True) if not line.startswith("#")]
paths = {}
for row in csv.DictReader(lines):
    src_node = int(row["src_node"])
    dst_group = int(row["dst_group"])
    paths.setdefault((src_node, dst_group), []).append(row)

checked = 0
missing = []
for src, dst in traffic_pairs:
    sg = rank_group(src)
    dg = rank_group(dst)
    if sg == dg:
        continue
    local_logical = src % logical_per_group
    src_node = sg * logical_per_group + local_logical
    ps = paths.get((src_node, dg), [])
    if not ps:
        missing.append((src, dst, src_node, dg))
        continue
    first_nodes = [int(x) for x in ps[0]["path_nodes"].split(";") if x]
    assert first_nodes[0] == src_node
    assert first_nodes[-1] // logical_per_group == dg
    checked += 1

assert not missing, missing[:5]
assert checked > 0
out = run / "sample_256_ksp_route_validation.txt"
out.write_text(
    "sample_256_ksp_route_validation OK\n"
    f"permutation_flows={len(traffic_pairs)}\n"
    f"cross_group_pairs_checked={checked}\n"
)
print(out)
PY

write_single_tm() {
  local tm="$1"
  local src="$2"
  local dst="$3"
  local size="$4"
  cat > "${tm}" <<EOF
Nodes 8192
Connections 1
${src}->${dst} id 1 start 0 size ${size}
EOF
}

run_htsim_case() {
  local name="$1"
  local tm="$2"
  local strat="$3"
  local sample_links="$4"
  local case_dir="${RUN_DIR}/${name}"
  mkdir -p "${case_dir}"
  cp "${tm}" "${case_dir}/traffic.cm"
  {
    echo "name=${name}"
    echo "topo=${HTSIM_TOPO}"
    echo "traffic=${case_dir}/traffic.cm"
    echo "strat=${strat}"
    echo "planes=${L1_PLANES}"
    echo "local_tray_size=8"
    echo "local_linkspeed_mbps=800000"
    echo "external_linkspeed_mbps=100000"
  } > "${case_dir}/run_config.txt"

  echo "  [htsim] ${name}"
  (
    cd "${case_dir}"
    if [[ "${sample_links}" == "1" ]]; then
      export HTSIM_LINK_LOAD_SAMPLE=1
      export HTSIM_LINK_LOAD_SAMPLE_US=100
    fi
    timeout "${HTSIM_TIMEOUT_SEC}" "${HTSIM_BIN}" \
      -topo "${HTSIM_TOPO}" \
      -tm "${case_dir}/traffic.cm" \
      -planes "${L1_PLANES}" \
      -strat "${strat}" \
      -load_balancing_algo ecmp \
      -linkspeed 100000 \
      -mtu 4150 \
      -q 50 \
      -local_tray_size 8 \
      -local_latency_ns 200 \
      -local_linkspeed 800000 \
      -end "${HTSIM_END_US}" \
      > "${case_dir}/htsim.log" 2>&1
  )
  test -s "${case_dir}/output_metrics/flowsInfo.csv"
}

if [[ "${RUN_HTSIM_SMOKE}" == "1" ]]; then
  echo "[6/7] HTSIM 8192-card bottom/local and ECMP smoke tests"
  write_single_tm "${RUN_DIR}/tm_local_direct.cm" "${LOCAL_SRC}" "${LOCAL_DST}" "${HTSIM_FLOW_SIZE_BYTES}"
  write_single_tm "${RUN_DIR}/tm_external_cross_group.cm" "${CROSS_SRC}" "${CROSS_DST}" "${HTSIM_FLOW_SIZE_BYTES}"
  write_single_tm "${RUN_DIR}/tm_same_group_cross_tray.cm" "${SAME_GROUP_SRC}" "${SAME_GROUP_DST}" "${HTSIM_FLOW_SIZE_BYTES}"

  run_htsim_case "htsim_local_direct_800g" "${RUN_DIR}/tm_local_direct.cm" ecmp_rr 1
  run_htsim_case "htsim_external_packet_rr_4planes" "${RUN_DIR}/tm_external_cross_group.cm" ecmp_rr 1
  run_htsim_case "htsim_external_flow_hash_4planes" "${RUN_DIR}/tm_external_cross_group.cm" ecmp_host 1
  run_htsim_case "htsim_same_group_cross_tray_packet_rr" "${RUN_DIR}/tm_same_group_cross_tray.cm" ecmp_rr 0

  if [[ "${RUN_SAMPLE_256_HTSIM}" == "1" ]]; then
    sed "s/ size 65536/ size ${HTSIM_SAMPLE_FLOW_SIZE_BYTES}/" \
      "${RUN_DIR}/sample_256_permutation.cm" > "${RUN_DIR}/sample_256_permutation_sized.cm"
    run_htsim_case "htsim_sample_256_permutation_packet_rr" \
      "${RUN_DIR}/sample_256_permutation_sized.cm" ecmp_rr 0
  fi
else
  echo "[6/7] Skip HTSIM smoke tests because RUN_HTSIM_SMOKE=0"
fi

echo "[7/7] Summarize results"
python3 - "${RUN_DIR}" <<'PY'
import csv
import sys
from pathlib import Path

run = Path(sys.argv[1])

def read_flows(path):
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))

def read_link_rates(case):
    info = case / "output_metrics" / "link_info.csv"
    loads = case / "output_metrics" / "link_load_1ms.csv"
    if not info.exists() or not loads.exists():
        return None
    infos = {}
    with info.open(newline="") as f:
        for row in csv.DictReader(f):
            infos[row["link_id"]] = row
    active = {}
    with loads.open(newline="") as f:
        for row in csv.DictReader(f):
            active[row["link_id"]] = active.get(row["link_id"], 0) + int(float(row["bytes"]))
    rows = []
    for link_id, bytes_ in active.items():
        if bytes_ > 0:
            row = dict(infos.get(link_id, {}))
            row["bytes"] = bytes_
            rows.append(row)
    return rows

summary = []
for name in [
    "htsim_local_direct_800g",
    "htsim_external_packet_rr_4planes",
    "htsim_external_flow_hash_4planes",
    "htsim_same_group_cross_tray_packet_rr",
    "htsim_sample_256_permutation_packet_rr",
]:
    case = run / name
    flows = read_flows(case / "output_metrics" / "flowsInfo.csv")
    if not flows:
        continue
    fcts = [float(r["fctNs"]) for r in flows]
    sizes = [int(r["flowSizeBytes"]) for r in flows]
    links = read_link_rates(case)
    if links is None:
        link_summary = "link_sampling=not_sampled"
    else:
        local_links = [r for r in links if r.get("link_name", "").startswith("LOCAL_")]
        rates = sorted({r.get("rate_gbps", "") for r in links})
        link_summary = (
            f"active_links={len(links)}, active_local_links={len(local_links)}, "
            f"active_rates_gbps={rates}"
        )
    summary.append(
        f"{name}: flows={len(flows)}, total_bytes={sum(sizes)}, "
        f"max_fct_ns={max(fcts):.3f}, {link_summary}"
    )

text = "\n".join([
    "OCS M=4,N=8 feature test completed",
    "",
    (run / "graph_validation.txt").read_text().strip(),
    "",
    (run / "spraypoint_validation.txt").read_text().strip(),
    "",
    (run / "ksp_validation.txt").read_text().strip(),
    "",
    (run / "sample_256_summary.txt").read_text().strip(),
    "",
    (run / "sample_256_ksp_route_validation.txt").read_text().strip(),
    "",
    "HTSIM smoke summaries:",
    *summary,
    "",
])
(run / "summary.txt").write_text(text)
print(run / "summary.txt")
PY

echo
echo "Done."
echo "  run_dir: ${RUN_DIR}"
echo "  summary: ${RUN_DIR}/summary.txt"
