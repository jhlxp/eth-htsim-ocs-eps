#!/usr/bin/env python3
"""Generate synthetic DeepSeek-R1 EP256 traffic from an empirical hotness CDF.

The empirical distribution is read from the provided decode_*.csv files:
each row is a MoE layer and each column is a local expert-slot hotness value.

For each synthetic R1 MoE layer, this script:
  1. pools all decode_*.csv values into one global empirical hotness CDF;
  2. writes that CDF to a text file;
  3. samples a fixed receiver-hotness distribution text file from that CDF;
  4. constructs traffic only from the sampled receiver-hotness text file;
  5. allocates src->dst dispatch tokens with equal per-source sends and
     empirical per-destination receives;
  6. builds ordered flow sizes where combine traffic is the reverse direction
     of dispatch traffic.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from bisect import bisect_left
from pathlib import Path
from typing import Iterable


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    workspace_root = repo_root.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--empirical-data-dir",
        type=Path,
        default=workspace_root / "需求" / "测试数据",
        help="Directory containing decode_0.csv ... decode_31.csv.",
    )
    parser.add_argument(
        "--empirical-distribution-csv",
        type=Path,
        default=None,
        help="Precomputed pooled distribution CSV with heat,count,pmf,cdf columns.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "deepseek_r1_empirical",
    )
    parser.add_argument("--ranks", type=int, default=256)
    parser.add_argument("--experts", type=int, default=256)
    parser.add_argument("--moe-layers", type=int, default=58)
    parser.add_argument("--topk", type=int, default=8)
    parser.add_argument("--hidden-size", type=int, default=7168)
    parser.add_argument("--dtype-bytes", type=int, default=2)
    parser.add_argument("--tokens-per-rank", type=int, default=4096)
    parser.add_argument(
        "--total-assignments-per-layer",
        type=int,
        default=0,
        help="Override total dispatch assignments per MoE layer. 0 keeps ranks*tokens_per_rank*topk.",
    )
    parser.add_argument(
        "--htsim-nodes",
        type=int,
        default=0,
        help="Node count written to the HTSIM .cm. 0 means the same as --ranks.",
    )
    parser.add_argument(
        "--rank-placement",
        choices=["contiguous", "strided", "random"],
        default="contiguous",
        help="Map logical EP ranks into physical HTSIM node IDs.",
    )
    parser.add_argument("--rank-offset", type=int, default=0)
    parser.add_argument("--placement-seed", type=int, default=42)
    parser.add_argument("--seed", type=int, default=20260624)
    parser.add_argument(
        "--shuffle-receivers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Shuffle sampled receiver hotness values per layer so hot experts rotate.",
    )
    parser.add_argument(
        "--include-combine",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include reverse-direction combine traffic in ordered flow sizes.",
    )
    parser.add_argument(
        "--skip-local-traffic",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exclude src==dst rows from the generated HTSIM connection matrix.",
    )
    parser.add_argument("--start-ps", type=int, default=0)
    return parser.parse_args()


def read_decode_layers(data_dir: Path) -> list[list[int]]:
    csv_paths = sorted(
        data_dir.glob("decode_*.csv"),
        key=lambda p: int(p.stem.split("_", 1)[1]),
    )
    if not csv_paths:
        raise FileNotFoundError(f"no decode_*.csv found in {data_dir}")

    layers: list[list[int]] | None = None
    for path in csv_paths:
        with path.open(newline="") as f:
            rows = [[int(x) for x in row if x != ""] for row in csv.reader(f)]
        if layers is None:
            layers = [[] for _ in rows]
        if len(rows) != len(layers):
            raise ValueError(f"{path} has {len(rows)} rows, expected {len(layers)}")
        for layer_id, row in enumerate(rows):
            layers[layer_id].extend(row)
    assert layers is not None
    return layers


def read_pooled_distribution_csv(path: Path) -> list[int]:
    values: list[int] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "heat" not in reader.fieldnames:
            raise ValueError(f"{path} must contain a 'heat' column")
        for row in reader:
            heat = int(float(row["heat"]))
            count = int(row.get("count") or 1)
            if count < 0:
                raise ValueError(f"{path}: negative count for heat={heat}")
            values.extend([heat] * count)
    if not values:
        raise ValueError(f"{path} produced no empirical values")
    return values


def read_empirical_source(args: argparse.Namespace) -> tuple[list[list[int]], list[int], str]:
    if args.empirical_distribution_csv is not None:
        pooled = read_pooled_distribution_csv(args.empirical_distribution_csv)
        return [pooled], pooled, str(args.empirical_distribution_csv)
    layers = read_decode_layers(args.empirical_data_dir)
    pooled = [v for layer in layers for v in layer]
    return layers, pooled, str(args.empirical_data_dir)


def quantile_sample(values: list[int], n: int) -> list[float]:
    if not values:
        raise ValueError("empty empirical values")
    sorted_values = sorted(values)
    m = len(sorted_values)
    if n == 1:
        return [float(sorted_values[m // 2])]
    sampled = []
    for i in range(n):
        # Midpoint quantile, using nearest-rank indexing.
        q = (i + 0.5) / n
        idx = min(m - 1, int(q * m))
        sampled.append(float(sorted_values[idx]))
    return sampled


def normalize_to_int_total(weights: list[float], total: int) -> list[int]:
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("weight sum must be positive")
    ideal = [w * total / weight_sum for w in weights]
    ints = [int(x) for x in ideal]
    rem = total - sum(ints)
    order = sorted(range(len(weights)), key=lambda i: ideal[i] - ints[i], reverse=True)
    for i in order[:rem]:
        ints[i] += 1
    return ints


def near_equal_row_totals(total: int, rows: int) -> list[int]:
    base = total // rows
    rem = total % rows
    return [base + (1 if i < rem else 0) for i in range(rows)]


def allocate_send_matrix(col_tokens: list[int], row_totals: list[int]) -> list[list[int]]:
    """Allocate a dense src->dst token matrix with exact row/column sums.

    Rows are equal or near-equal sends. Columns are the desired empirical receives.
    Diagonal traffic is allowed here; it can be removed from network outputs
    later if desired.
    """
    n = len(col_tokens)
    total = sum(row_totals)
    if sum(col_tokens) != total:
        raise ValueError(f"column total {sum(col_tokens)} != row total {total}")

    base_cols = [c // n for c in col_tokens]
    col_rems = [c % n for c in col_tokens]
    row_base = sum(base_cols)
    row_rems = [row_total - row_base for row_total in row_totals]
    if any(rem < 0 for rem in row_rems):
        raise ValueError("row totals are too small for the column base allocation")
    if sum(row_rems) != sum(col_rems):
        raise AssertionError("remainder mismatch")

    matrix = [base_cols.copy() for _ in range(n)]
    src_cursor = 0
    for dst, rem in enumerate(col_rems):
        while rem:
            while row_rems[src_cursor] == 0:
                src_cursor = (src_cursor + 1) % n
            matrix[src_cursor][dst] += 1
            row_rems[src_cursor] -= 1
            rem -= 1
            src_cursor = (src_cursor + 1) % n

    for src, expected in enumerate(row_totals):
        if sum(matrix[src]) != expected:
            raise AssertionError(f"row {src} sum mismatch")
    for dst, expected in enumerate(col_tokens):
        if sum(matrix[src][dst] for src in range(n)) != expected:
            raise AssertionError(f"column {dst} sum mismatch")
    return matrix


def build_rank_mapping(ranks: int, htsim_nodes: int, placement: str, offset: int, seed: int) -> list[int]:
    if htsim_nodes < ranks:
        raise ValueError(f"htsim_nodes {htsim_nodes} must be >= ranks {ranks}")
    if offset < 0 or offset >= htsim_nodes:
        raise ValueError(f"rank_offset {offset} outside [0, {htsim_nodes})")
    if placement == "contiguous":
        if offset + ranks > htsim_nodes:
            raise ValueError("contiguous rank placement exceeds htsim_nodes")
        return list(range(offset, offset + ranks))
    if placement == "strided":
        stride = htsim_nodes // ranks
        if stride == 0:
            raise ValueError("strided rank placement needs htsim_nodes >= ranks")
        mapping = [(offset + i * stride) % htsim_nodes for i in range(ranks)]
        if len(set(mapping)) != ranks:
            raise ValueError("strided rank placement produced duplicate physical ranks")
        return mapping
    if placement == "random":
        rng = random.Random(seed)
        return sorted(rng.sample(range(htsim_nodes), ranks))
    raise ValueError(f"unknown placement: {placement}")


def quantiles(values: list[int], qs: Iterable[float]) -> dict[str, int]:
    if not values:
        return {f"p{int(q * 100):02d}": 0 for q in qs}
    vals = sorted(values)
    out: dict[str, int] = {}
    for q in qs:
        idx = min(len(vals) - 1, int(round(q * (len(vals) - 1))))
        out[f"p{int(q * 100):02d}"] = vals[idx]
    return out


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, int | float | str]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_empirical_cdfs(out_dir: Path, empirical_layers: list[list[int]]) -> None:
    raw_values = [v for layer in empirical_layers for v in layer]
    sorted_raw = sorted(raw_values)
    raw_rows = [
        {
            "rank": i + 1,
            "heat": value,
            "cdf": (i + 1) / len(sorted_raw),
        }
        for i, value in enumerate(sorted_raw)
    ]
    write_csv(out_dir / "empirical_raw_heat_cdf.csv", ["rank", "heat", "cdf"], raw_rows)
    with (out_dir / "empirical_pooled_heat_cdf.txt").open("w") as f:
        f.write("# rank heat cdf\n")
        for row in raw_rows:
            f.write(f"{row['rank']} {row['heat']} {row['cdf']:.12g}\n")

    share_values: list[float] = []
    for layer in empirical_layers:
        total = sum(layer)
        share_values.extend(v / total for v in layer)
    sorted_share = sorted(share_values)
    share_rows = [
        {
            "rank": i + 1,
            "share": value,
            "cdf": (i + 1) / len(sorted_share),
        }
        for i, value in enumerate(sorted_share)
    ]
    write_csv(out_dir / "empirical_layer_share_cdf.csv", ["rank", "share", "cdf"], share_rows)


def write_sampled_receiver_hotness(
    path: Path,
    pooled_empirical: list[int],
    layers: int,
    experts: int,
    seed: int,
    shuffle_receivers: bool,
) -> None:
    with path.open("w") as f:
        f.write("# layer dst_rank hotness\n")
        for layer_id in range(layers):
            weights = quantile_sample(pooled_empirical, experts)
            if shuffle_receivers:
                rng = random.Random(seed + layer_id)
                rng.shuffle(weights)
            for dst_rank, hotness in enumerate(weights):
                f.write(f"{layer_id} {dst_rank} {hotness:g}\n")


def read_sampled_receiver_hotness(path: Path, layers: int, experts: int) -> list[list[float]]:
    sampled = [[0.0 for _ in range(experts)] for _ in range(layers)]
    seen = [[False for _ in range(experts)] for _ in range(layers)]
    with path.open() as f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) != 3:
                raise ValueError(f"{path}:{lineno}: expected 'layer dst_rank hotness'")
            layer_id = int(parts[0])
            dst_rank = int(parts[1])
            hotness = float(parts[2])
            if not (0 <= layer_id < layers):
                raise ValueError(f"{path}:{lineno}: layer {layer_id} outside [0, {layers})")
            if not (0 <= dst_rank < experts):
                raise ValueError(f"{path}:{lineno}: dst_rank {dst_rank} outside [0, {experts})")
            sampled[layer_id][dst_rank] = hotness
            seen[layer_id][dst_rank] = True

    missing = [
        (layer_id, dst_rank)
        for layer_id in range(layers)
        for dst_rank in range(experts)
        if not seen[layer_id][dst_rank]
    ]
    if missing:
        layer_id, dst_rank = missing[0]
        raise ValueError(f"{path}: missing sampled hotness for layer={layer_id}, dst_rank={dst_rank}")
    return sampled


def main() -> None:
    args = parse_args()
    if args.ranks != args.experts:
        raise SystemExit("this compact EP256 generator currently requires ranks == experts")
    htsim_nodes = args.htsim_nodes or args.ranks
    rank_mapping = build_rank_mapping(
        args.ranks,
        htsim_nodes,
        args.rank_placement,
        args.rank_offset,
        args.placement_seed,
    )
    default_assignments_per_src = args.tokens_per_rank * args.topk
    total_assignments_per_layer = (
        args.total_assignments_per_layer
        if args.total_assignments_per_layer > 0
        else args.ranks * default_assignments_per_src
    )
    row_totals_per_layer = near_equal_row_totals(total_assignments_per_layer, args.ranks)
    payload_bytes = args.hidden_size * args.dtype_bytes

    empirical_layers, pooled_empirical, empirical_source = read_empirical_source(args)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_empirical_cdfs(args.out_dir, empirical_layers)
    sampled_hotness_path = args.out_dir / "sampled_receiver_hotness_distribution.txt"
    write_sampled_receiver_hotness(
        sampled_hotness_path,
        pooled_empirical,
        args.moe_layers,
        args.experts,
        args.seed,
        args.shuffle_receivers,
    )
    sampled_hotness = read_sampled_receiver_hotness(sampled_hotness_path, args.moe_layers, args.experts)

    aggregate_dispatch = [[0 for _ in range(args.ranks)] for _ in range(args.ranks)]
    receiver_rows: list[dict[str, int | float | str]] = []

    for layer_id in range(args.moe_layers):
        weights = sampled_hotness[layer_id]
        dst_tokens = normalize_to_int_total(weights, total_assignments_per_layer)
        layer_matrix = allocate_send_matrix(dst_tokens, row_totals_per_layer)
        for src in range(args.ranks):
            for dst in range(args.ranks):
                aggregate_dispatch[src][dst] += layer_matrix[src][dst]

        for dst, tokens in enumerate(dst_tokens):
            receiver_rows.append(
                {
                    "layer": layer_id,
                    "dst_rank": dst,
                    "dst_physical_rank": rank_mapping[dst],
                    "dst_expert": dst,
                    "receive_tokens": tokens,
                    "receive_dispatch_bytes": tokens * payload_bytes,
                    "receive_total_bytes_with_combine": tokens * payload_bytes * (2 if args.include_combine else 1),
                }
            )

    write_csv(
        args.out_dir / "expert_receive_by_layer.csv",
        [
            "layer",
            "dst_rank",
            "dst_physical_rank",
            "dst_expert",
            "receive_tokens",
            "receive_dispatch_bytes",
            "receive_total_bytes_with_combine",
        ],
        receiver_rows,
    )

    all_pair_rows = []
    network_pair_rows = []
    network_sizes: list[int] = []
    all_sizes: list[int] = []
    for src in range(args.ranks):
        for dst in range(args.ranks):
            dispatch_tokens = aggregate_dispatch[src][dst]
            combine_tokens = aggregate_dispatch[dst][src] if args.include_combine else 0
            total_tokens = dispatch_tokens + combine_tokens
            row = {
                "src_rank": src,
                "src_physical_rank": rank_mapping[src],
                "src_expert": src,
                "dst_rank": dst,
                "dst_physical_rank": rank_mapping[dst],
                "dst_expert": dst,
                "is_local": int(src == dst),
                "dispatch_tokens_all_layers": dispatch_tokens,
                "combine_tokens_all_layers": combine_tokens,
                "total_tokens_all_layers": total_tokens,
                "dispatch_bytes_all_layers": dispatch_tokens * payload_bytes,
                "combine_bytes_all_layers": combine_tokens * payload_bytes,
                "total_bytes_all_layers": total_tokens * payload_bytes,
            }
            all_pair_rows.append(row)
            all_sizes.append(int(row["total_bytes_all_layers"]))
            if total_tokens > 0 and not (args.skip_local_traffic and src == dst):
                network_pair_rows.append(row)
                network_sizes.append(int(row["total_bytes_all_layers"]))

    fields = [
        "src_rank",
        "src_physical_rank",
        "src_expert",
        "dst_rank",
        "dst_physical_rank",
        "dst_expert",
        "is_local",
        "dispatch_tokens_all_layers",
        "combine_tokens_all_layers",
        "total_tokens_all_layers",
        "dispatch_bytes_all_layers",
        "combine_bytes_all_layers",
        "total_bytes_all_layers",
    ]
    write_csv(args.out_dir / "expert2expert_empirical_all_pairs.csv", fields, all_pair_rows)
    write_csv(args.out_dir / "expert2expert_empirical_network_pairs.csv", fields, network_pair_rows)

    flow_cdf_rows = [
        {
            "rank": i + 1,
            "total_bytes_all_layers": value,
            "cdf": (i + 1) / len(network_sizes),
        }
        for i, value in enumerate(sorted(network_sizes))
    ]
    write_csv(
        args.out_dir / "expert2expert_flow_size_cdf.csv",
        ["rank", "total_bytes_all_layers", "cdf"],
        flow_cdf_rows,
    )

    total_network_bytes = sum(network_sizes)
    total_all_bytes = sum(all_sizes)
    q = quantiles(network_sizes, [0, 0.5, 0.9, 0.95, 0.99, 1])
    summary_rows = [
        {
            "metric": "network_flow_count",
            "value": len(network_sizes),
        },
        {
            "metric": "network_total_bytes",
            "value": total_network_bytes,
        },
        {
            "metric": "all_pairs_total_bytes_including_local",
            "value": total_all_bytes,
        },
        {
            "metric": "network_min_bytes",
            "value": q["p00"],
        },
        {
            "metric": "network_p50_bytes",
            "value": q["p50"],
        },
        {
            "metric": "network_p90_bytes",
            "value": q["p90"],
        },
        {
            "metric": "network_p95_bytes",
            "value": q["p95"],
        },
        {
            "metric": "network_p99_bytes",
            "value": q["p99"],
        },
        {
            "metric": "network_max_bytes",
            "value": q["p100"],
        },
    ]
    write_csv(args.out_dir / "flow_size_summary.csv", ["metric", "value"], summary_rows)

    tm_path = args.out_dir / "deepseek_r1_ep256_empirical_aggregate.cm"
    with tm_path.open("w") as f:
        f.write(f"Nodes {htsim_nodes}\n")
        f.write(f"Connections {len(network_pair_rows)}\n")
        for flow_id, row in enumerate(network_pair_rows, start=1):
            f.write(
                f"{row['src_physical_rank']}->{row['dst_physical_rank']} id {flow_id} "
                f"start {args.start_ps} size {row['total_bytes_all_layers']}\n"
            )

    write_csv(
        args.out_dir / "rank_mapping.csv",
        ["logical_rank", "physical_rank"],
        [
            {"logical_rank": logical_rank, "physical_rank": physical_rank}
            for logical_rank, physical_rank in enumerate(rank_mapping)
        ],
    )

    config = {
        "model": "deepseek-r1",
        "tp": 1,
        "pp": 1,
        "dp": 1,
        "ep": args.ranks,
        "ranks": args.ranks,
        "htsim_nodes": htsim_nodes,
        "hosts": args.ranks,
        "gpus_per_host": 1,
        "rank_placement": args.rank_placement,
        "rank_offset": args.rank_offset,
        "placement_seed": args.placement_seed,
        "experts_per_moe_layer": args.experts,
        "experts_per_rank": 1,
        "moe_layers": args.moe_layers,
        "topk": args.topk,
        "hidden_size": args.hidden_size,
        "dtype_bytes": args.dtype_bytes,
        "tokens_per_rank_per_layer": args.tokens_per_rank,
        "payload_bytes_per_token_per_expert": payload_bytes,
        "default_assignments_per_src_rank_per_layer": default_assignments_per_src,
        "row_assignments_per_src_rank_per_layer_min": min(row_totals_per_layer),
        "row_assignments_per_src_rank_per_layer_max": max(row_totals_per_layer),
        "total_assignments_per_layer": total_assignments_per_layer,
        "total_assignments_source": "override" if args.total_assignments_per_layer > 0 else "tokens_per_rank_times_topk",
        "routing_distribution": "pooled_empirical_cdf_from_all_decode_csv",
        "empirical_sampling": "midpoint_quantiles_from_pooled_cdf",
        "empirical_pooled_value_count": len(pooled_empirical),
        "sampled_receiver_hotness_distribution": str(sampled_hotness_path),
        "empirical_data_dir": str(args.empirical_data_dir),
        "empirical_distribution_csv": (
            str(args.empirical_distribution_csv) if args.empirical_distribution_csv else ""
        ),
        "empirical_source": empirical_source,
        "seed": args.seed,
        "shuffle_receivers": args.shuffle_receivers,
        "include_combine": args.include_combine,
        "combine_direction": "reverse_of_dispatch",
        "skip_local_traffic": args.skip_local_traffic,
    }
    (args.out_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")

    readme = f"""DeepSeek-R1 empirical-CDF synthetic summary
===========================================
Input empirical source: {empirical_source}
Output directory: {args.out_dir}

Model:
  TP/PP/DP/EP: 1/1/1/{args.ranks}
  ranks/hosts/GPUs: {args.ranks}
  HTSIM physical nodes: {htsim_nodes}
  rank placement: {args.rank_placement}
  MoE layers: {args.moe_layers}
  experts per layer: {args.experts}
  experts per rank: 1
  topk: {args.topk}
  tokens per rank per layer: {args.tokens_per_rank}
  payload bytes per token per selected expert: {payload_bytes}
  total assignments per layer: {total_assignments_per_layer}
  per-source assignments per layer: {min(row_totals_per_layer)}..{max(row_totals_per_layer)}

Construction:
  receiver hotness follows one pooled empirical CDF built from all decode_*.csv values
  pooled empirical value count: {len(pooled_empirical)}
  pooled CDF txt: empirical_pooled_heat_cdf.txt
  sampled receiver distribution txt: sampled_receiver_hotness_distribution.txt
  traffic construction reads sampled_receiver_hotness_distribution.txt
  source sends are near-equal at {min(row_totals_per_layer)}..{max(row_totals_per_layer)} assignments per source per layer
  combine traffic is {'included as reverse-direction dispatch traffic' if args.include_combine else 'excluded'}
  local src==dst pairs are {'excluded' if args.skip_local_traffic else 'included'} from the HTSIM .cm

Network ordered flow sizes over {args.moe_layers} MoE layer(s):
  count: {len(network_sizes)}
  min: {q['p00']} bytes
  p50: {q['p50']} bytes
  p90: {q['p90']} bytes
  p95: {q['p95']} bytes
  p99: {q['p99']} bytes
  max: {q['p100']} bytes
  total: {total_network_bytes} bytes

Generated files:
  config.json
  empirical_pooled_heat_cdf.txt
  sampled_receiver_hotness_distribution.txt
  empirical_raw_heat_cdf.csv
  empirical_layer_share_cdf.csv
  expert_receive_by_layer.csv
  expert2expert_empirical_all_pairs.csv
  expert2expert_empirical_network_pairs.csv
  expert2expert_flow_size_cdf.csv
  flow_size_summary.csv
  rank_mapping.csv
  deepseek_r1_ep256_empirical_aggregate.cm
"""
    (args.out_dir / "README.txt").write_text(readme)
    print(readme)


if __name__ == "__main__":
    main()
