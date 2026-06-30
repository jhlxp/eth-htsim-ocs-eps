#!/usr/bin/env python3
"""Pod-local LPT traffic-engineering model.

This model splits a flow into two independent endpoint decisions:

1. source-side LPT: bucket by source pod and choose the source L0/L1 EPS;
2. destination-side LPT: bucket by destination pod and choose the destination L0/L1 EPS.

The two endpoint decisions are then joined by flow id to form a source route
from source L1 EPS to destination L1 EPS.  This matches the deployment idea
where each pod can independently balance its egress and ingress choices, while
OCS is treated as a direct cross-pod L1-to-L1 pipe.
"""

from __future__ import annotations

import argparse
import ctypes
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from lpt_te_model import (
    LptAssignment,
    PlacedFlow,
    TopologyCfg,
    candidate_set_imbalance,
    cpp_lpt_lib,
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


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    htsim_root = here.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution-csv", type=Path, default=htsim_root / "tests/data/empirical_pooled_distribution.csv")
    parser.add_argument("--out-dir", type=Path, default=here / "lpt_pod_local_results")
    parser.add_argument("--pod-counts", type=int, nargs="+", default=[16, 8, 4, 2])
    parser.add_argument("--trials", type=int, default=16)
    parser.add_argument("--placement-seed", type=int, default=42)
    parser.add_argument("--traffic-seed", type=int, default=20260624)
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


def configure_pod_local_core() -> ctypes.CDLL:
    lib = cpp_lpt_lib()
    lib.lpt_assign_pod_local_core.argtypes = [
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.uint64, flags="C_CONTIGUOUS"),
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        np.ctypeslib.ndpointer(dtype=np.uint64, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.uint64, flags="C_CONTIGUOUS"),
        ctypes.c_size_t,
        np.ctypeslib.ndpointer(dtype=np.uint64, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.uint64, flags="C_CONTIGUOUS"),
        ctypes.c_size_t,
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS"),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.lpt_assign_pod_local_core.restype = ctypes.c_int
    return lib


def pod_local_lpt_assign(
    placed_flows: list[PlacedFlow],
    cfg: TopologyCfg,
    record_assignments: bool,
) -> tuple[list[LptAssignment], dict[int, int], dict[int, int], dict[int, int], dict[int, int], dict[str, float]]:
    lib = configure_pod_local_core()
    flow_count = len(placed_flows)
    l0_count = (cfg.nodes // cfg.ranks_per_tray) * cfg.l1_planes
    l1_count = cfg.groups * cfg.l1_planes * cfg.l1_eps_per_l1_plane

    src_ranks = np.fromiter((f.src_rank for f in placed_flows), dtype=np.int32, count=flow_count)
    dst_ranks = np.fromiter((f.dst_rank for f in placed_flows), dtype=np.int32, count=flow_count)
    flow_bytes = np.fromiter((f.bytes for f in placed_flows), dtype=np.uint64, count=flow_count)

    src_l0_load = np.zeros(l0_count, dtype=np.uint64)
    dst_l0_load = np.zeros(l0_count, dtype=np.uint64)
    src_l1_load = np.zeros(l1_count, dtype=np.uint64)
    dst_l1_load = np.zeros(l1_count, dtype=np.uint64)
    src_l0_ids = np.empty(flow_count, dtype=np.int32)
    dst_l0_ids = np.empty(flow_count, dtype=np.int32)
    src_l1_ids = np.empty(flow_count, dtype=np.int32)
    dst_l1_ids = np.empty(flow_count, dtype=np.int32)
    src_l0_planes = np.empty(flow_count, dtype=np.int32)
    dst_l0_planes = np.empty(flow_count, dtype=np.int32)
    src_l1_eps_ids = np.empty(flow_count, dtype=np.int32)
    dst_l1_planes = np.empty(flow_count, dtype=np.int32)
    dst_l1_eps_ids = np.empty(flow_count, dtype=np.int32)

    src_sort_ms = ctypes.c_double()
    src_greedy_ms = ctypes.c_double()
    dst_sort_ms = ctypes.c_double()
    dst_greedy_ms = ctypes.c_double()
    wall_ms = ctypes.c_double()

    rc = lib.lpt_assign_pod_local_core(
        src_ranks,
        dst_ranks,
        flow_bytes,
        flow_count,
        cfg.nodes,
        cfg.ranks_per_group,
        cfg.ranks_per_tray,
        cfg.l1_planes,
        cfg.l1_eps_per_l1_plane,
        src_l0_load,
        dst_l0_load,
        l0_count,
        src_l1_load,
        dst_l1_load,
        l1_count,
        src_l0_ids,
        dst_l0_ids,
        src_l1_ids,
        dst_l1_ids,
        src_l0_planes,
        dst_l0_planes,
        src_l1_eps_ids,
        dst_l1_planes,
        dst_l1_eps_ids,
        ctypes.byref(src_sort_ms),
        ctypes.byref(src_greedy_ms),
        ctypes.byref(dst_sort_ms),
        ctypes.byref(dst_greedy_ms),
        ctypes.byref(wall_ms),
    )
    if rc:
        raise RuntimeError(f"lpt_assign_pod_local_core failed with code {rc}")

    assignments: list[LptAssignment] = []
    if record_assignments:
        for idx, f in enumerate(placed_flows):
            src_tray = f.src_rank // cfg.ranks_per_tray
            src_group = src_tray // cfg.trays_per_group
            dst_tray = f.dst_rank // cfg.ranks_per_tray
            dst_group = f.dst_rank // cfg.ranks_per_group
            assignments.append(
                LptAssignment(
                    src_logical=f.src_logical,
                    dst_logical=f.dst_logical,
                    src_rank=f.src_rank,
                    dst_rank=f.dst_rank,
                    bytes=f.bytes,
                    src_group=src_group,
                    src_tray=src_tray,
                    dst_group=dst_group,
                    dst_tray=dst_tray,
                    src_l0_plane=int(src_l0_planes[idx]),
                    src_l0_id=int(src_l0_ids[idx]),
                    dst_l0_plane=int(dst_l0_planes[idx]),
                    dst_l0_id=int(dst_l0_ids[idx]),
                    src_l1_eps=int(src_l1_eps_ids[idx]),
                    src_l1_id=int(src_l1_ids[idx]),
                    dst_l1_plane=int(dst_l1_planes[idx]),
                    dst_l1_eps=int(dst_l1_eps_ids[idx]),
                    dst_l1_id=int(dst_l1_ids[idx]),
                )
            )

    cpu_ms = src_sort_ms.value + src_greedy_ms.value + dst_sort_ms.value + dst_greedy_ms.value
    return (
        assignments,
        {idx: int(load) for idx, load in enumerate(src_l0_load) if load},
        {idx: int(load) for idx, load in enumerate(dst_l0_load) if load},
        {idx: int(load) for idx, load in enumerate(src_l1_load) if load},
        {idx: int(load) for idx, load in enumerate(dst_l1_load) if load},
        {
            "src_sort_ms": src_sort_ms.value,
            "src_greedy_ms": src_greedy_ms.value,
            "dst_sort_ms": dst_sort_ms.value,
            "dst_greedy_ms": dst_greedy_ms.value,
            "cpu_ms": cpu_ms,
            "wall_ms": wall_ms.value,
        },
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
        "model": "pod-local-src-dst-lpt",
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
            assignments, src_l0_load, dst_l0_load, src_l1_load, dst_l1_load, lpt_cost = pod_local_lpt_assign(
                placed,
                cfg,
                record_assignments=(trial == args.detail_trial),
            )

            active_trays = sorted({rank_tray(r, cfg) for r in placement})
            active_groups = sorted({rank_group(r, cfg) for r in placement})
            src_l0_active_ids = [l0_id_for_tray_plane(t, p, cfg) for t in active_trays for p in range(cfg.l1_planes)]
            dst_l0_active_ids = [l0_id_for_tray_plane(t, p, cfg) for t in active_trays for p in range(cfg.l1_planes)]
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
                "lpt_sort_ms": f"{(lpt_cost['src_sort_ms'] + lpt_cost['dst_sort_ms']):.6f}",
                "lpt_greedy_ms": f"{(lpt_cost['src_greedy_ms'] + lpt_cost['dst_greedy_ms']):.6f}",
                "lpt_total_ms": f"{lpt_cost['wall_ms']:.6f}",
                "lpt_wall_ms": f"{lpt_cost['wall_ms']:.6f}",
                "lpt_cpu_ms": f"{lpt_cost['cpu_ms']:.6f}",
                "lpt_src_sort_ms": f"{lpt_cost['src_sort_ms']:.6f}",
                "lpt_src_greedy_ms": f"{lpt_cost['src_greedy_ms']:.6f}",
                "lpt_dst_sort_ms": f"{lpt_cost['dst_sort_ms']:.6f}",
                "lpt_dst_greedy_ms": f"{lpt_cost['dst_greedy_ms']:.6f}",
                "lpt_total_us_per_flow": f"{(lpt_cost['wall_ms'] * 1000.0 / len(logical_flows)):.6f}",
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
        "flow_count",
        "lpt_sort_ms", "lpt_greedy_ms", "lpt_total_ms", "lpt_wall_ms", "lpt_cpu_ms",
        "lpt_src_sort_ms", "lpt_src_greedy_ms", "lpt_dst_sort_ms", "lpt_dst_greedy_ms",
        "lpt_total_us_per_flow", "total_gib",
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
    write_csv(out_dir / "lpt_summary_by_trial.csv", fields, summary_rows)

    agg_rows = []
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
        "lpt_sort_ms", "lpt_greedy_ms", "lpt_total_ms", "lpt_wall_ms", "lpt_cpu_ms",
        "lpt_src_sort_ms", "lpt_src_greedy_ms", "lpt_dst_sort_ms", "lpt_dst_greedy_ms",
        "lpt_total_us_per_flow",
    ]
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
    write_csv(out_dir / "lpt_summary_aggregate.csv", ["pod_count", "metric", "mean", "min", "max", "std"], agg_rows)

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

    readme = f"""# Pod-local LPT TE 理论建模结果

这个模型把发送侧和接收侧隔离：

1. 按 `src_group` 分桶，在每个发送 pod 内独立执行 LPT，决定源侧 L0/L1 EPS。
2. 按 `dst_group` 分桶，在每个接收 pod 内独立执行 LPT，决定目的侧 L0/L1 EPS。
3. 每条 flow 最终用 `(src L1 EPS, dst L1 EPS)` 作为源路由端点。

这对应 EP 组内收集 flow 信息后，按 pod 并行计算局部 TE 的方案。16 个 active pod 时，可拆成 16 个发送侧任务和 16 个接收侧任务。

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
        wall = np.mean([float(r["lpt_wall_ms"]) for r in rows])
        cpu = np.mean([float(r["lpt_cpu_ms"]) for r in rows])
        print(
            f"pods {pod_count}: avg src L0 {src_l0:.4f}, src L1 {src_l1:.4f}, "
            f"dst L0 {dst_l0:.4f}, dst L1 {dst_l1:.4f}, wall {wall:.4f} ms, cpu {cpu:.4f} ms"
        )


if __name__ == "__main__":
    main()
