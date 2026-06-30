#!/usr/bin/env python3
"""ECMP/hash baseline for EP256 all-to-all on the Huawei 8192 topology.

This is the single-flow baseline paired with the LPT TE models.  It uses the
same traffic generation and rank placement code, but chooses every L0/L1 EPS
with a deterministic per-flow hash instead of load-aware greedy placement.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import Counter
from pathlib import Path

import numpy as np

from lpt_te_model import (
    LptAssignment,
    TopologyCfg,
    candidate_set_imbalance,
    generate_flows,
    gib,
    l0_id_for_tray_plane,
    l1_id_for_group_plane_eps,
    mib,
    place_flows,
    plot_cdfs,
    plot_l0_eps_load_by_pod,
    plot_l1_eps_load_by_pod,
    plot_pod_loads,
    plot_summary,
    random_place_ranks,
    rank_group,
    rank_tray,
    read_distribution_values,
    stats,
    tray_group,
    write_and_plot_l1_pair_matrix,
    write_csv,
)


MASK64 = (1 << 64) - 1


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    htsim_root = here.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution-csv", type=Path, default=htsim_root / "tests/data/empirical_pooled_distribution.csv")
    parser.add_argument("--out-dir", type=Path, default=here / "ecmp_results")
    parser.add_argument("--pod-counts", type=int, nargs="+", default=[16, 8, 4, 2])
    parser.add_argument("--trials", type=int, default=16)
    parser.add_argument("--placement-seed", type=int, default=42)
    parser.add_argument("--traffic-seed", type=int, default=20260624)
    parser.add_argument("--ecmp-seed", type=int, default=20260630)
    parser.add_argument("--ep-ranks", type=int, default=256)
    parser.add_argument("--tokens-per-rank", type=int, default=4096)
    parser.add_argument("--topk", type=int, default=8)
    parser.add_argument("--hidden-size", type=int, default=7168)
    parser.add_argument("--dtype-bytes", type=int, default=2)
    parser.add_argument("--moe-layers", type=int, default=1)
    parser.add_argument("--include-combine", action="store_true")
    parser.add_argument("--nodes", type=int, default=8192)
    parser.add_argument("--groups", type=int, default=16)
    parser.add_argument("--ranks-per-group", type=int, default=512)
    parser.add_argument("--ranks-per-tray", type=int, default=8)
    parser.add_argument("--l1-planes", type=int, default=4)
    parser.add_argument("--l1-eps-per-l1-plane", type=int, default=4)
    parser.add_argument("--detail-trial", type=int, default=0)
    return parser.parse_args()


def mix64(x: int) -> int:
    x &= MASK64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & MASK64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & MASK64
    x ^= x >> 31
    return x & MASK64


def flow_hash(seed: int, salt: int, *values: int) -> int:
    h = mix64(seed ^ (salt * 0x9E3779B97F4A7C15))
    for value in values:
        h = mix64(h ^ (int(value) + 0x9E3779B97F4A7C15))
    return h


def ecmp_choice(modulus: int, seed: int, salt: int, *values: int) -> int:
    if modulus <= 0:
        raise ValueError("modulus must be positive")
    return flow_hash(seed, salt, *values) % modulus


def ecmp_assign(
    placed_flows,
    cfg: TopologyCfg,
    seed: int,
    record_assignments: bool,
):
    src_l0_load: dict[int, int] = Counter()
    dst_l0_load: dict[int, int] = Counter()
    src_l1_load: dict[int, int] = Counter()
    dst_l1_load: dict[int, int] = Counter()
    assignments: list[LptAssignment] = []

    start = time.perf_counter()
    for idx, flow in enumerate(placed_flows):
        src_tray = flow.src_rank // cfg.ranks_per_tray
        src_group = tray_group(src_tray, cfg)
        dst_tray = flow.dst_rank // cfg.ranks_per_tray
        dst_group = flow.dst_rank // cfg.ranks_per_group

        key = (flow.src_rank, flow.dst_rank, flow.src_logical, flow.dst_logical)
        src_l0_plane = ecmp_choice(cfg.l1_planes, seed, 0, idx, *key)
        src_l0_id = l0_id_for_tray_plane(src_tray, src_l0_plane, cfg)
        src_l1_eps = ecmp_choice(cfg.l1_eps_per_l1_plane, seed, 1, idx, *key)
        src_l1_id = l1_id_for_group_plane_eps(src_group, src_l0_plane, src_l1_eps, cfg)

        dst_l0_plane = ecmp_choice(cfg.l1_planes, seed, 2, idx, *key)
        dst_l0_id = l0_id_for_tray_plane(dst_tray, dst_l0_plane, cfg)
        dst_l1_local = ecmp_choice(cfg.l1_planes * cfg.l1_eps_per_l1_plane, seed, 3, idx, *key)
        dst_l1_plane = dst_l1_local // cfg.l1_eps_per_l1_plane
        dst_l1_eps = dst_l1_local % cfg.l1_eps_per_l1_plane
        dst_l1_id = l1_id_for_group_plane_eps(dst_group, dst_l1_plane, dst_l1_eps, cfg)

        src_l0_load[src_l0_id] += flow.bytes
        dst_l0_load[dst_l0_id] += flow.bytes
        src_l1_load[src_l1_id] += flow.bytes
        dst_l1_load[dst_l1_id] += flow.bytes

        if record_assignments:
            assignments.append(
                LptAssignment(
                    src_logical=flow.src_logical,
                    dst_logical=flow.dst_logical,
                    src_rank=flow.src_rank,
                    dst_rank=flow.dst_rank,
                    bytes=flow.bytes,
                    src_group=src_group,
                    src_tray=src_tray,
                    dst_group=dst_group,
                    dst_tray=dst_tray,
                    src_l0_plane=src_l0_plane,
                    src_l0_id=src_l0_id,
                    dst_l0_plane=dst_l0_plane,
                    dst_l0_id=dst_l0_id,
                    src_l1_eps=src_l1_eps,
                    src_l1_id=src_l1_id,
                    dst_l1_plane=dst_l1_plane,
                    dst_l1_eps=dst_l1_eps,
                    dst_l1_id=dst_l1_id,
                )
            )

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return (
        assignments,
        dict(src_l0_load),
        dict(dst_l0_load),
        dict(src_l1_load),
        dict(dst_l1_load),
        {"ecmp_assign_ms": elapsed_ms},
    )


def main() -> None:
    args = parse_args()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = TopologyCfg(
        nodes=args.nodes,
        groups=args.groups,
        ranks_per_group=args.ranks_per_group,
        ranks_per_tray=args.ranks_per_tray,
        l1_planes=args.l1_planes,
        l1_eps_per_l1_plane=args.l1_eps_per_l1_plane,
    )
    if cfg.nodes != cfg.groups * cfg.ranks_per_group:
        raise ValueError("nodes must equal groups*ranks_per_group")
    if cfg.ranks_per_group % cfg.ranks_per_tray:
        raise ValueError("ranks_per_group must be divisible by ranks_per_tray")

    payload_bytes = args.hidden_size * args.dtype_bytes
    values = read_distribution_values(args.distribution_csv)
    logical_flows = generate_flows(
        values,
        args.ep_ranks,
        args.moe_layers,
        args.tokens_per_rank,
        args.topk,
        payload_bytes,
        args.traffic_seed,
        args.include_combine,
    )
    total_bytes = sum(f.bytes for f in logical_flows)

    traffic_summary = {
        "model": "ecmp-hash-single-flow",
        "ep_ranks": args.ep_ranks,
        "flow_count": len(logical_flows),
        "total_bytes": total_bytes,
        "total_gib": gib(total_bytes),
        "payload_bytes_per_assignment": payload_bytes,
        "tokens_per_rank": args.tokens_per_rank,
        "topk": args.topk,
        "moe_layers": args.moe_layers,
        "include_combine": args.include_combine,
        "distribution_csv": str(args.distribution_csv),
        "ecmp_seed": args.ecmp_seed,
    }
    (out_dir / "traffic_summary.json").write_text(json.dumps(traffic_summary, indent=2) + "\n")

    summary_rows: list[dict] = []
    detail_loads_for_cdf: dict[int, dict[str, list[int]]] = {}
    pod_detail_rows: list[dict] = []

    for pod_count in args.pod_counts:
        for trial in range(args.trials):
            rng = random.Random(args.placement_seed + 1009 * pod_count + trial)
            placement = random_place_ranks(args.ep_ranks, pod_count, cfg, rng)
            placed = place_flows(logical_flows, placement)
            assignments, src_l0_load, dst_l0_load, src_l1_load, dst_l1_load, ecmp_cost = ecmp_assign(
                placed,
                cfg,
                args.ecmp_seed,
                record_assignments=(trial == args.detail_trial),
            )

            active_trays = sorted({rank_tray(r, cfg) for r in placement})
            active_groups = sorted({rank_group(r, cfg) for r in placement})
            src_l0_active_ids = [l0_id_for_tray_plane(t, p, cfg) for t in active_trays for p in range(cfg.l1_planes)]
            dst_l0_active_ids = src_l0_active_ids
            src_l1_active_ids = [
                l1_id_for_group_plane_eps(g, p, e, cfg)
                for g in active_groups
                for p in range(cfg.l1_planes)
                for e in range(cfg.l1_eps_per_l1_plane)
            ]
            dst_l1_active_ids = src_l1_active_ids

            src_l0_values = [src_l0_load.get(x, 0) for x in src_l0_active_ids]
            dst_l0_values = [dst_l0_load.get(x, 0) for x in dst_l0_active_ids]
            src_l1_values = [src_l1_load.get(x, 0) for x in src_l1_active_ids]
            dst_l1_values = [dst_l1_load.get(x, 0) for x in dst_l1_active_ids]
            src_l0_stats = stats(src_l0_values)
            dst_l0_stats = stats(dst_l0_values)
            src_l1_stats = stats(src_l1_values)
            dst_l1_stats = stats(dst_l1_values)
            src_l0_tray_balance = candidate_set_imbalance(
                src_l0_load,
                [[l0_id_for_tray_plane(t, p, cfg) for p in range(cfg.l1_planes)] for t in active_trays],
            )
            dst_l0_tray_balance = candidate_set_imbalance(
                dst_l0_load,
                [[l0_id_for_tray_plane(t, p, cfg) for p in range(cfg.l1_planes)] for t in active_trays],
            )
            src_l1_group_plane_balance = candidate_set_imbalance(
                src_l1_load,
                [
                    [l1_id_for_group_plane_eps(g, p, e, cfg) for e in range(cfg.l1_eps_per_l1_plane)]
                    for g in active_groups
                    for p in range(cfg.l1_planes)
                ],
            )
            src_l1_pod_balance = candidate_set_imbalance(
                src_l1_load,
                [
                    [
                        l1_id_for_group_plane_eps(g, p, e, cfg)
                        for p in range(cfg.l1_planes)
                        for e in range(cfg.l1_eps_per_l1_plane)
                    ]
                    for g in active_groups
                ],
            )
            dst_l1_pod_balance = candidate_set_imbalance(
                dst_l1_load,
                [
                    [
                        l1_id_for_group_plane_eps(g, p, e, cfg)
                        for p in range(cfg.l1_planes)
                        for e in range(cfg.l1_eps_per_l1_plane)
                    ]
                    for g in active_groups
                ],
            )

            row = {
                "pod_count": pod_count,
                "trial": trial,
                "active_groups": len(active_groups),
                "active_trays": len(active_trays),
                "active_src_l0_eps": len(src_l0_active_ids),
                "active_dst_l0_eps": len(dst_l0_active_ids),
                "active_src_l1_eps": len(src_l1_active_ids),
                "active_dst_l1_eps": len(dst_l1_active_ids),
                "flow_count": len(logical_flows),
                "ecmp_assign_ms": f"{ecmp_cost['ecmp_assign_ms']:.6f}",
                "ecmp_us_per_flow": f"{(ecmp_cost['ecmp_assign_ms'] * 1000.0 / len(logical_flows)):.6f}",
                "total_gib": f"{gib(total_bytes):.6f}",
                "src_l0_mean_gib": f"{gib(src_l0_stats['mean']):.6f}",
                "src_l0_p95_gib": f"{gib(src_l0_stats['p95']):.6f}",
                "src_l0_max_gib": f"{gib(src_l0_stats['max']):.6f}",
                "src_l0_std_gib": f"{gib(src_l0_stats['std']):.6f}",
                "src_l0_imbalance_max_mean": f"{src_l0_stats['imbalance_max_mean']:.6f}",
                "src_l0_in_tray_imbalance_mean": f"{src_l0_tray_balance['mean']:.6f}",
                "src_l0_in_tray_imbalance_p95": f"{src_l0_tray_balance['p95']:.6f}",
                "src_l0_in_tray_imbalance_max": f"{src_l0_tray_balance['max']:.6f}",
                "dst_l0_mean_gib": f"{gib(dst_l0_stats['mean']):.6f}",
                "dst_l0_p95_gib": f"{gib(dst_l0_stats['p95']):.6f}",
                "dst_l0_max_gib": f"{gib(dst_l0_stats['max']):.6f}",
                "dst_l0_std_gib": f"{gib(dst_l0_stats['std']):.6f}",
                "dst_l0_imbalance_max_mean": f"{dst_l0_stats['imbalance_max_mean']:.6f}",
                "dst_l0_in_tray_imbalance_mean": f"{dst_l0_tray_balance['mean']:.6f}",
                "dst_l0_in_tray_imbalance_p95": f"{dst_l0_tray_balance['p95']:.6f}",
                "dst_l0_in_tray_imbalance_max": f"{dst_l0_tray_balance['max']:.6f}",
                "src_l1_mean_gib": f"{gib(src_l1_stats['mean']):.6f}",
                "src_l1_p95_gib": f"{gib(src_l1_stats['p95']):.6f}",
                "src_l1_max_gib": f"{gib(src_l1_stats['max']):.6f}",
                "src_l1_std_gib": f"{gib(src_l1_stats['std']):.6f}",
                "src_l1_imbalance_max_mean": f"{src_l1_stats['imbalance_max_mean']:.6f}",
                "src_l1_in_pod_imbalance_mean": f"{src_l1_pod_balance['mean']:.6f}",
                "src_l1_in_pod_imbalance_p95": f"{src_l1_pod_balance['p95']:.6f}",
                "src_l1_in_pod_imbalance_max": f"{src_l1_pod_balance['max']:.6f}",
                "src_l1_in_group_plane_imbalance_mean": f"{src_l1_group_plane_balance['mean']:.6f}",
                "src_l1_in_group_plane_imbalance_p95": f"{src_l1_group_plane_balance['p95']:.6f}",
                "src_l1_in_group_plane_imbalance_max": f"{src_l1_group_plane_balance['max']:.6f}",
                "dst_l1_mean_gib": f"{gib(dst_l1_stats['mean']):.6f}",
                "dst_l1_p95_gib": f"{gib(dst_l1_stats['p95']):.6f}",
                "dst_l1_max_gib": f"{gib(dst_l1_stats['max']):.6f}",
                "dst_l1_std_gib": f"{gib(dst_l1_stats['std']):.6f}",
                "dst_l1_imbalance_max_mean": f"{dst_l1_stats['imbalance_max_mean']:.6f}",
                "dst_l1_in_pod_imbalance_mean": f"{dst_l1_pod_balance['mean']:.6f}",
                "dst_l1_in_pod_imbalance_p95": f"{dst_l1_pod_balance['p95']:.6f}",
                "dst_l1_in_pod_imbalance_max": f"{dst_l1_pod_balance['max']:.6f}",
            }
            summary_rows.append(row)

            if trial == args.detail_trial:
                detail_loads_for_cdf[pod_count] = {
                    "src_l0": src_l0_values,
                    "dst_l0": dst_l0_values,
                    "src_l1": src_l1_values,
                    "dst_l1": dst_l1_values,
                }
                for kind, loads, ids in [
                    ("src_l0", src_l0_load, src_l0_active_ids),
                    ("dst_l0", dst_l0_load, dst_l0_active_ids),
                ]:
                    write_csv(
                        out_dir / f"{kind}_loads_pods{pod_count}_trial{trial}.csv",
                        ["l0_id", "tray", "group", "plane", "load_bytes", "load_gib"],
                        [
                            {
                                "l0_id": x,
                                "tray": x // cfg.l1_planes,
                                "group": tray_group(x // cfg.l1_planes, cfg),
                                "plane": x % cfg.l1_planes,
                                "load_bytes": loads.get(x, 0),
                                "load_gib": f"{gib(loads.get(x, 0)):.9f}",
                            }
                            for x in ids
                        ],
                    )
                for kind, loads, ids in [
                    ("src_l1", src_l1_load, src_l1_active_ids),
                    ("dst_l1", dst_l1_load, dst_l1_active_ids),
                ]:
                    write_csv(
                        out_dir / f"{kind}_loads_pods{pod_count}_trial{trial}.csv",
                        ["l1_id", "group", "plane", "eps", "load_bytes", "load_gib"],
                        [
                            {
                                "l1_id": x,
                                "group": x // (cfg.l1_planes * cfg.l1_eps_per_l1_plane),
                                "plane": (x // cfg.l1_eps_per_l1_plane) % cfg.l1_planes,
                                "eps": x % cfg.l1_eps_per_l1_plane,
                                "load_bytes": loads.get(x, 0),
                                "load_gib": f"{gib(loads.get(x, 0)):.9f}",
                            }
                            for x in ids
                        ],
                    )
                write_csv(
                    out_dir / f"assignments_pods{pod_count}_trial{trial}.csv",
                    [
                        "src_logical", "dst_logical", "src_rank", "dst_rank", "bytes", "mib",
                        "src_group", "src_tray", "dst_group", "dst_tray",
                        "src_l0_plane", "src_l0_id", "dst_l0_plane", "dst_l0_id",
                        "src_l1_eps", "src_l1_id", "dst_l1_plane", "dst_l1_eps", "dst_l1_id",
                    ],
                    [
                        {
                            "src_logical": a.src_logical,
                            "dst_logical": a.dst_logical,
                            "src_rank": a.src_rank,
                            "dst_rank": a.dst_rank,
                            "bytes": a.bytes,
                            "mib": f"{mib(a.bytes):.6f}",
                            "src_group": a.src_group,
                            "src_tray": a.src_tray,
                            "dst_group": a.dst_group,
                            "dst_tray": a.dst_tray,
                            "src_l0_plane": a.src_l0_plane,
                            "src_l0_id": a.src_l0_id,
                            "dst_l0_plane": a.dst_l0_plane,
                            "dst_l0_id": a.dst_l0_id,
                            "src_l1_eps": a.src_l1_eps,
                            "src_l1_id": a.src_l1_id,
                            "dst_l1_plane": a.dst_l1_plane,
                            "dst_l1_eps": a.dst_l1_eps,
                            "dst_l1_id": a.dst_l1_id,
                        }
                        for a in assignments
                    ],
                )
                write_and_plot_l1_pair_matrix(out_dir, pod_count, trial, assignments, active_groups, cfg)
                egress = Counter()
                ingress = Counter()
                for pf in placed:
                    egress[rank_group(pf.src_rank, cfg)] += pf.bytes
                    ingress[rank_group(pf.dst_rank, cfg)] += pf.bytes
                for pod in range(pod_count):
                    active_ranks = sum(1 for rank in placement if rank_group(rank, cfg) == pod)
                    pod_detail_rows.append({
                        "pod_count": pod_count,
                        "trial": trial,
                        "pod": pod,
                        "active_ranks": active_ranks,
                        "egress_bytes": egress[pod],
                        "ingress_bytes": ingress[pod],
                        "egress_gib": f"{gib(egress[pod]):.6f}",
                        "ingress_gib": f"{gib(ingress[pod]):.6f}",
                    })

    fields = [
        "pod_count", "trial", "active_groups", "active_trays",
        "active_src_l0_eps", "active_dst_l0_eps",
        "active_src_l1_eps", "active_dst_l1_eps",
        "flow_count", "ecmp_assign_ms", "ecmp_us_per_flow", "total_gib",
        "src_l0_mean_gib", "src_l0_p95_gib", "src_l0_max_gib", "src_l0_std_gib",
        "src_l0_imbalance_max_mean", "src_l0_in_tray_imbalance_mean",
        "src_l0_in_tray_imbalance_p95", "src_l0_in_tray_imbalance_max",
        "dst_l0_mean_gib", "dst_l0_p95_gib", "dst_l0_max_gib", "dst_l0_std_gib",
        "dst_l0_imbalance_max_mean", "dst_l0_in_tray_imbalance_mean",
        "dst_l0_in_tray_imbalance_p95", "dst_l0_in_tray_imbalance_max",
        "src_l1_mean_gib", "src_l1_p95_gib", "src_l1_max_gib", "src_l1_std_gib",
        "src_l1_imbalance_max_mean", "src_l1_in_pod_imbalance_mean",
        "src_l1_in_pod_imbalance_p95", "src_l1_in_pod_imbalance_max",
        "src_l1_in_group_plane_imbalance_mean", "src_l1_in_group_plane_imbalance_p95",
        "src_l1_in_group_plane_imbalance_max",
        "dst_l1_mean_gib", "dst_l1_p95_gib", "dst_l1_max_gib", "dst_l1_std_gib",
        "dst_l1_imbalance_max_mean", "dst_l1_in_pod_imbalance_mean",
        "dst_l1_in_pod_imbalance_p95", "dst_l1_in_pod_imbalance_max",
    ]
    write_csv(out_dir / "ecmp_summary_by_trial.csv", fields, summary_rows)

    metrics = [
        "src_l0_imbalance_max_mean", "dst_l0_imbalance_max_mean",
        "src_l1_imbalance_max_mean", "src_l1_in_pod_imbalance_mean",
        "src_l1_in_pod_imbalance_p95", "src_l1_in_pod_imbalance_max",
        "dst_l1_imbalance_max_mean", "dst_l1_in_pod_imbalance_mean",
        "dst_l1_in_pod_imbalance_p95", "dst_l1_in_pod_imbalance_max",
        "src_l0_in_tray_imbalance_mean", "src_l0_in_tray_imbalance_p95",
        "src_l0_in_tray_imbalance_max", "dst_l0_in_tray_imbalance_mean",
        "dst_l0_in_tray_imbalance_p95", "dst_l0_in_tray_imbalance_max",
        "src_l1_in_group_plane_imbalance_mean", "src_l1_in_group_plane_imbalance_p95",
        "src_l1_in_group_plane_imbalance_max",
        "src_l0_max_gib", "dst_l0_max_gib", "src_l1_max_gib", "dst_l1_max_gib",
        "src_l0_p95_gib", "dst_l0_p95_gib", "src_l1_p95_gib", "dst_l1_p95_gib",
        "ecmp_assign_ms", "ecmp_us_per_flow",
    ]
    agg_rows = []
    for pod_count in args.pod_counts:
        rows = [r for r in summary_rows if int(r["pod_count"]) == pod_count]
        for metric in metrics:
            vals = np.array([float(r[metric]) for r in rows], dtype=np.float64)
            agg_rows.append({
                "pod_count": pod_count,
                "metric": metric,
                "mean": f"{vals.mean():.6f}",
                "min": f"{vals.min():.6f}",
                "max": f"{vals.max():.6f}",
                "std": f"{vals.std():.6f}",
            })
    write_csv(out_dir / "ecmp_summary_aggregate.csv", ["pod_count", "metric", "mean", "min", "max", "std"], agg_rows)

    if pod_detail_rows:
        write_csv(
            out_dir / "traffic_by_pod_detail_trial.csv",
            ["pod_count", "trial", "pod", "active_ranks", "egress_bytes", "ingress_bytes", "egress_gib", "ingress_gib"],
            pod_detail_rows,
        )

    plot_summary(summary_rows, out_dir)
    plot_cdfs(detail_loads_for_cdf, out_dir)
    plot_pod_loads(pod_detail_rows, out_dir)
    plot_l0_eps_load_by_pod(out_dir, args.pod_counts, args.detail_trial, cfg, "src", "Source")
    plot_l0_eps_load_by_pod(out_dir, args.pod_counts, args.detail_trial, cfg, "dst", "Destination")
    plot_l1_eps_load_by_pod(out_dir, args.pod_counts, args.detail_trial, cfg, "src", "Source")
    plot_l1_eps_load_by_pod(out_dir, args.pod_counts, args.detail_trial, cfg, "dst", "Destination")
    for old, new in [
        ("lpt_summary_by_pod_count.png", "ecmp_summary_by_pod_count.png"),
        ("lpt_eps_load_cdf_detail_trial.png", "ecmp_eps_load_cdf_detail_trial.png"),
    ]:
        old_path = out_dir / old
        if old_path.exists():
            old_path.rename(out_dir / new)

    readme = f"""# ECMP/hash baseline 结果

这个目录是单 flow、非 spray 的 ECMP/hash baseline。

每条 flow 独立 hash 到：

1. 源 tray 的一个 L0 plane；
2. 源 group / plane 内的一个 L1 EPS；
3. 目的 tray 的一个 L0 plane；
4. 目的 group 内的一个 L1 EPS。

这不是负载感知算法，只表示传统 flow-level ECMP 在相同流量和 placement 下的理论分布。

输入流量来自 `{args.distribution_csv}`，EP{args.ep_ranks}，`tokens_per_rank={args.tokens_per_rank}`，`topk={args.topk}`。

总流量：{gib(total_bytes):.3f} GiB，flow 数：{len(logical_flows)}。
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")

    print(f"wrote {out_dir}")
    print(f"flows {len(logical_flows)} total_gib {gib(total_bytes):.3f}")
    for pod_count in args.pod_counts:
        rows = [r for r in summary_rows if int(r["pod_count"]) == pod_count]
        src_l0 = np.mean([float(r["src_l0_imbalance_max_mean"]) for r in rows])
        src_l1 = np.mean([float(r["src_l1_imbalance_max_mean"]) for r in rows])
        dst_l0 = np.mean([float(r["dst_l0_imbalance_max_mean"]) for r in rows])
        dst_l1 = np.mean([float(r["dst_l1_imbalance_max_mean"]) for r in rows])
        assign_ms = np.mean([float(r["ecmp_assign_ms"]) for r in rows])
        print(
            f"pods {pod_count}: avg src L0 {src_l0:.4f}, src L1 {src_l1:.4f}, "
            f"dst L0 {dst_l0:.4f}, dst L1 {dst_l1:.4f}, assign {assign_ms:.4f} ms"
        )


if __name__ == "__main__":
    main()
