#!/usr/bin/env python3
"""LPT traffic-engineering model for EP256 all-to-all on the Huawei 8192 topology.

The model is intentionally offline/theoretical:
- generate EP256 dispatch-only all-to-all flows from the pooled empirical expert-hotness CDF;
- randomly place the 256 active ranks inside K selected pods/groups of the 8192-rank fabric;
- route each whole flow through exactly one source L0 EPS, one source L1 EPS, and one destination L1 EPS;
- use Longest Processing Time first (LPT) greedy assignment:
    1. choose the least-loaded L0 plane attached to the source tray;
    2. choose the least-loaded source L1 EPS in the selected source group/plane;
    3. independently choose the least-loaded destination L1 EPS in the destination group.

This is a lower-bound style TE model for single-path flows. It does not simulate
UEC congestion control, receiver-side incast queues, or packet spray.
"""

from __future__ import annotations

import argparse
import ctypes
import csv
import json
import math
import random
import subprocess
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np


_CPP_LPT_LIB: ctypes.CDLL | None = None


@dataclass(frozen=True)
class TopologyCfg:
    nodes: int = 8192
    groups: int = 16
    ranks_per_group: int = 512
    ranks_per_tray: int = 8
    l1_planes: int = 4
    l1_eps_per_l1_plane: int = 4

    @property
    def trays_per_group(self) -> int:
        return self.ranks_per_group // self.ranks_per_tray

    @property
    def total_trays(self) -> int:
        return self.nodes // self.ranks_per_tray


@dataclass(frozen=True)
class Flow:
    src_logical: int
    dst_logical: int
    bytes: int


@dataclass(frozen=True)
class PlacedFlow:
    src_logical: int
    dst_logical: int
    src_rank: int
    dst_rank: int
    bytes: int


@dataclass(frozen=True)
class LptAssignment:
    src_logical: int
    dst_logical: int
    src_rank: int
    dst_rank: int
    bytes: int
    src_group: int
    src_tray: int
    dst_group: int
    dst_tray: int
    src_l0_plane: int
    src_l0_id: int
    dst_l0_plane: int
    dst_l0_id: int
    src_l1_eps: int
    src_l1_id: int
    dst_l1_plane: int
    dst_l1_eps: int
    dst_l1_id: int


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    htsim_root = here.parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution-csv", type=Path, default=htsim_root / "tests/data/empirical_pooled_distribution.csv")
    parser.add_argument("--out-dir", type=Path, default=here / "lpt_results")
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
    parser.add_argument("--detail-trial", type=int, default=0, help="Write detailed load CSVs for this trial index.")
    return parser.parse_args()


def read_distribution_values(path: Path) -> list[int]:
    values: list[int] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "heat" not in reader.fieldnames:
            raise ValueError(f"{path} must contain heat,count columns")
        for row in reader:
            heat = int(float(row["heat"]))
            count = int(row.get("count") or 1)
            values.extend([heat] * count)
    if not values:
        raise ValueError(f"empty distribution: {path}")
    return values


def quantile_sample(values: list[int], n: int) -> list[float]:
    vals = sorted(values)
    m = len(vals)
    if n == 1:
        return [float(vals[m // 2])]
    out = []
    for i in range(n):
        q = (i + 0.5) / n
        out.append(float(vals[min(m - 1, int(q * m))]))
    return out


def normalize_to_int_total(weights: list[float], total: int) -> list[int]:
    s = sum(weights)
    if s <= 0:
        raise ValueError("weight sum must be positive")
    ideal = [w * total / s for w in weights]
    ints = [int(x) for x in ideal]
    rem = total - sum(ints)
    order = sorted(range(len(weights)), key=lambda i: ideal[i] - ints[i], reverse=True)
    for i in order[:rem]:
        ints[i] += 1
    return ints


def near_equal_row_totals(total: int, n: int) -> list[int]:
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def allocate_send_matrix(col_tokens: list[int], row_totals: list[int]) -> list[list[int]]:
    n = len(col_tokens)
    if sum(col_tokens) != sum(row_totals):
        raise ValueError("row/column totals mismatch")
    base_cols = [c // n for c in col_tokens]
    col_rems = [c % n for c in col_tokens]
    row_base = sum(base_cols)
    row_rems = [r - row_base for r in row_totals]
    if min(row_rems) < 0:
        raise ValueError("row totals too small")
    mat = [base_cols.copy() for _ in range(n)]
    src = 0
    for dst, rem0 in enumerate(col_rems):
        rem = rem0
        while rem:
            while row_rems[src] == 0:
                src = (src + 1) % n
            mat[src][dst] += 1
            row_rems[src] -= 1
            rem -= 1
            src = (src + 1) % n
    return mat


def generate_flows(
    distribution_values: list[int],
    ep_ranks: int,
    moe_layers: int,
    tokens_per_rank: int,
    topk: int,
    payload_bytes: int,
    seed: int,
    include_combine: bool,
) -> list[Flow]:
    total_assignments = ep_ranks * tokens_per_rank * topk
    row_totals = near_equal_row_totals(total_assignments, ep_ranks)
    aggregate = [[0 for _ in range(ep_ranks)] for _ in range(ep_ranks)]
    for layer in range(moe_layers):
        weights = quantile_sample(distribution_values, ep_ranks)
        rng = random.Random(seed + layer)
        rng.shuffle(weights)
        dst_tokens = normalize_to_int_total(weights, total_assignments)
        layer_mat = allocate_send_matrix(dst_tokens, row_totals)
        for src in range(ep_ranks):
            for dst in range(ep_ranks):
                aggregate[src][dst] += layer_mat[src][dst]

    flows: list[Flow] = []
    for src in range(ep_ranks):
        for dst in range(ep_ranks):
            if src == dst:
                continue
            tokens = aggregate[src][dst]
            if include_combine:
                tokens += aggregate[dst][src]
            if tokens > 0:
                flows.append(Flow(src, dst, tokens * payload_bytes))
    return flows


def rank_group(rank: int, cfg: TopologyCfg) -> int:
    return rank // cfg.ranks_per_group


def rank_tray(rank: int, cfg: TopologyCfg) -> int:
    return rank // cfg.ranks_per_tray


def tray_group(tray: int, cfg: TopologyCfg) -> int:
    return tray // cfg.trays_per_group


def l0_id_for_tray_plane(tray: int, plane: int, cfg: TopologyCfg) -> int:
    return tray * cfg.l1_planes + plane


def l1_id_for_group_plane_eps(group: int, plane: int, eps: int, cfg: TopologyCfg) -> int:
    return (group * cfg.l1_planes + plane) * cfg.l1_eps_per_l1_plane + eps


def cpp_lpt_lib() -> ctypes.CDLL:
    global _CPP_LPT_LIB
    if _CPP_LPT_LIB is not None:
        return _CPP_LPT_LIB

    here = Path(__file__).resolve().parent
    src = here / "lpt_core.cpp"
    build_dir = here / ".build"
    lib_path = build_dir / "lpt_core.so"
    if not src.exists():
        raise FileNotFoundError(src)
    if not lib_path.exists() or lib_path.stat().st_mtime < src.stat().st_mtime:
        build_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", "-pthread", str(src), "-o", str(lib_path)],
            check=True,
        )

    lib = ctypes.CDLL(str(lib_path))
    lib.lpt_assign_core.argtypes = [
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
    ]
    lib.lpt_assign_core.restype = ctypes.c_int
    _CPP_LPT_LIB = lib
    return lib


def random_place_ranks(ep_ranks: int, pod_count: int, cfg: TopologyCfg, rng: random.Random) -> list[int]:
    if pod_count > cfg.groups:
        raise ValueError(f"pod_count {pod_count} > groups {cfg.groups}")
    selected_groups = list(range(pod_count))
    candidates = []
    for g in selected_groups:
        start = g * cfg.ranks_per_group
        candidates.extend(range(start, start + cfg.ranks_per_group))
    if ep_ranks > len(candidates):
        raise ValueError("not enough ranks in selected pods")
    # Random placement without replacement. The sorted order keeps logical rank IDs stable across reruns.
    return sorted(rng.sample(candidates, ep_ranks))


def place_flows(flows: list[Flow], placement: list[int]) -> list[PlacedFlow]:
    return [PlacedFlow(f.src_logical, f.dst_logical, placement[f.src_logical], placement[f.dst_logical], f.bytes) for f in flows]


def lpt_assign(
    placed_flows: list[PlacedFlow],
    cfg: TopologyCfg,
    record_assignments: bool,
) -> tuple[list[LptAssignment], dict[int, int], dict[int, int], dict[int, int], dict[int, int], dict[str, float]]:
    lib = cpp_lpt_lib()
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
    sort_ms = ctypes.c_double()
    greedy_ms = ctypes.c_double()

    rc = lib.lpt_assign_core(
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
        ctypes.byref(sort_ms),
        ctypes.byref(greedy_ms),
    )
    if rc:
        raise RuntimeError(f"lpt_assign_core failed with code {rc}")

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

    return (
        assignments,
        {idx: int(load) for idx, load in enumerate(src_l0_load) if load},
        {idx: int(load) for idx, load in enumerate(dst_l0_load) if load},
        {idx: int(load) for idx, load in enumerate(src_l1_load) if load},
        {idx: int(load) for idx, load in enumerate(dst_l1_load) if load},
        {
            "sort_ms": sort_ms.value,
            "greedy_ms": greedy_ms.value,
            "total_ms": sort_ms.value + greedy_ms.value,
        },
    )


def stats(values: Iterable[int]) -> dict[str, float]:
    arr = np.array(list(values), dtype=np.float64)
    if arr.size == 0:
        return {k: 0.0 for k in ["count", "sum", "mean", "p50", "p95", "p99", "max", "min", "std", "imbalance_max_mean"]}
    mean = float(arr.mean())
    return {
        "count": float(arr.size),
        "sum": float(arr.sum()),
        "mean": mean,
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "max": float(arr.max()),
        "min": float(arr.min()),
        "std": float(arr.std()),
        "imbalance_max_mean": float(arr.max() / mean) if mean else 0.0,
    }


def candidate_set_imbalance(loads: dict[int, int], candidate_sets: list[list[int]]) -> dict[str, float]:
    ratios: list[float] = []
    for candidates in candidate_sets:
        vals = np.array([loads.get(x, 0) for x in candidates], dtype=np.float64)
        mean = float(vals.mean()) if vals.size else 0.0
        if mean > 0:
            ratios.append(float(vals.max() / mean))
    if not ratios:
        return {"mean": 0.0, "max": 0.0, "p95": 0.0}
    arr = np.array(ratios, dtype=np.float64)
    return {
        "mean": float(arr.mean()),
        "max": float(arr.max()),
        "p95": float(np.percentile(arr, 95)),
    }


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def mib(x: float) -> float:
    return x / (1024.0 * 1024.0)


def gib(x: float) -> float:
    return x / (1024.0 * 1024.0 * 1024.0)


def plot_summary(summary_rows: list[dict], out_dir: Path) -> None:
    # Trial-averaged max/mean load ratios by pod count.
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in summary_rows:
        grouped[int(row["pod_count"])].append(row)
    pod_counts = sorted(grouped.keys(), reverse=True)
    src_l0_ratio = [np.mean([float(r["src_l0_imbalance_max_mean"]) for r in grouped[p]]) for p in pod_counts]
    dst_l0_ratio = [np.mean([float(r["dst_l0_imbalance_max_mean"]) for r in grouped[p]]) for p in pod_counts]
    src_l1_ratio = [np.mean([float(r["src_l1_imbalance_max_mean"]) for r in grouped[p]]) for p in pod_counts]
    dst_l1_ratio = [np.mean([float(r["dst_l1_imbalance_max_mean"]) for r in grouped[p]]) for p in pod_counts]
    src_l0_max = [np.mean([float(r["src_l0_max_gib"]) for r in grouped[p]]) for p in pod_counts]
    dst_l0_max = [np.mean([float(r["dst_l0_max_gib"]) for r in grouped[p]]) for p in pod_counts]
    src_l1_max = [np.mean([float(r["src_l1_max_gib"]) for r in grouped[p]]) for p in pod_counts]
    dst_l1_max = [np.mean([float(r["dst_l1_max_gib"]) for r in grouped[p]]) for p in pod_counts]

    x = np.arange(len(pod_counts))
    width = 0.2
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    axes[0].bar(x - 1.5 * width, src_l0_ratio, width, label="src L0 max/mean", color="#4C78A8")
    axes[0].bar(x - 0.5 * width, src_l1_ratio, width, label="src L1 max/mean", color="#72B7B2")
    axes[0].bar(x + 0.5 * width, dst_l0_ratio, width, label="dst L0 max/mean", color="#F58518")
    axes[0].bar(x + 1.5 * width, dst_l1_ratio, width, label="dst L1 max/mean", color="#E45756")
    axes[0].set_xticks(x, [str(p) for p in pod_counts])
    axes[0].set_xlabel("active pods")
    axes[0].set_ylabel("imbalance ratio")
    axes[0].set_title("LPT residual imbalance")
    axes[0].grid(axis="y", alpha=0.25)
    axes[0].legend()

    axes[1].bar(x - 1.5 * width, src_l0_max, width, label="src L0 max", color="#4C78A8")
    axes[1].bar(x - 0.5 * width, src_l1_max, width, label="src L1 max", color="#72B7B2")
    axes[1].bar(x + 0.5 * width, dst_l0_max, width, label="dst L0 max", color="#F58518")
    axes[1].bar(x + 1.5 * width, dst_l1_max, width, label="dst L1 max", color="#E45756")
    axes[1].set_xticks(x, [str(p) for p in pod_counts])
    axes[1].set_xlabel("active pods")
    axes[1].set_ylabel("GiB")
    axes[1].set_title("Average max EPS load")
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend()

    fig.tight_layout()
    fig.savefig(out_dir / "lpt_summary_by_pod_count.png", dpi=180)
    plt.close(fig)


def plot_cdfs(load_details: dict[int, dict[str, list[int]]], out_dir: Path) -> None:
    pod_counts = sorted(load_details.keys(), reverse=True)
    layers = [
        ("src_l0", "src L0", "#4C78A8"),
        ("src_l1", "src L1", "#2A9D8F"),
        ("dst_l0", "dst L0", "#F58518"),
        ("dst_l1", "dst L1", "#F58518"),
    ]
    fig, axes = plt.subplots(len(pod_counts), len(layers), figsize=(18, 2.65 * len(pod_counts)), sharex=False)
    if len(pod_counts) == 1:
        axes = np.array([axes])
    for row_idx, p in enumerate(pod_counts):
        for col_idx, (layer, label, color) in enumerate(layers):
            ax = axes[row_idx][col_idx]
            vals = np.array(sorted(load_details[p][layer]), dtype=np.float64) / (1024.0 ** 3)
            if vals.size:
                y = np.arange(1, vals.size + 1) / vals.size
                ax.plot(vals, y, color=color, lw=2)
                ax.axvline(vals.mean(), color="#D62728", ls="--", lw=1, label=f"mean {vals.mean():.2f} GiB")
                ax.axvline(vals.max(), color="#444444", ls=":", lw=1, label=f"max {vals.max():.2f} GiB")
            ax.set_title(f"{p} pods - {label} EPS load CDF")
            ax.set_xlabel("load (GiB)")
            ax.set_ylabel("CDF")
            ax.grid(alpha=0.25)
            ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / "lpt_eps_load_cdf_detail_trial.png", dpi=180)
    plt.close(fig)


def plot_pod_loads(pod_rows: list[dict], out_dir: Path) -> None:
    # Detailed trial only: source egress and destination ingress by group/pod.
    by_pod_count: dict[int, list[dict]] = defaultdict(list)
    for row in pod_rows:
        by_pod_count[int(row["pod_count"])].append(row)
    pod_counts = sorted(by_pod_count.keys(), reverse=True)
    fig, axes = plt.subplots(len(pod_counts), 1, figsize=(11, 2.5 * len(pod_counts)), sharex=True)
    if len(pod_counts) == 1:
        axes = [axes]
    for ax, p in zip(axes, pod_counts):
        rows = sorted(by_pod_count[p], key=lambda r: int(r["pod"]))
        pods = [int(r["pod"]) for r in rows]
        eg = [float(r["egress_gib"]) for r in rows]
        ing = [float(r["ingress_gib"]) for r in rows]
        x = np.arange(len(pods))
        width = 0.38
        ax.bar(x - width / 2, eg, width, label="src egress", color="#4C78A8")
        ax.bar(x + width / 2, ing, width, label="dst ingress", color="#F58518")
        ax.set_xticks(x, [str(v) for v in pods])
        ax.set_ylabel("GiB")
        ax.set_title(f"{p} active pods: traffic by pod (detail trial)")
        ax.grid(axis="y", alpha=0.25)
        ax.legend(fontsize=8)
    axes[-1].set_xlabel("pod/group id")
    fig.tight_layout()
    fig.savefig(out_dir / "traffic_by_pod_detail_trial.png", dpi=180)
    plt.close(fig)


def plot_l1_eps_load_by_pod(
    out_dir: Path,
    pod_counts: list[int],
    detail_trial: int,
    cfg: TopologyCfg,
    kind: str,
    label: str,
) -> None:
    labels = [f"P{p}E{e}" for p in range(cfg.l1_planes) for e in range(cfg.l1_eps_per_l1_plane)]
    palette = ["#4C78A8", "#72B7B2", "#F58518", "#54A24B"]
    bar_colors = []
    for plane in range(cfg.l1_planes):
        bar_colors.extend([palette[plane % len(palette)]] * cfg.l1_eps_per_l1_plane)

    for pod_count in pod_counts:
        csv_path = out_dir / f"{kind}_l1_loads_pods{pod_count}_trial{detail_trial}.csv"
        if not csv_path.exists():
            continue

        by_pod: dict[int, list[dict]] = defaultdict(list)
        with csv_path.open(newline="") as f:
            for row in csv.DictReader(f):
                row["group"] = int(row["group"])
                row["plane"] = int(row["plane"])
                row["eps"] = int(row["eps"])
                row["load_gib"] = float(row["load_gib"])
                by_pod[row["group"]].append(row)

        pods = sorted(by_pod)
        if not pods:
            continue

        pod_values: dict[int, list[float]] = {}
        max_y = 0.0
        for pod in pods:
            vals = [0.0] * (cfg.l1_planes * cfg.l1_eps_per_l1_plane)
            for row in by_pod[pod]:
                idx = row["plane"] * cfg.l1_eps_per_l1_plane + row["eps"]
                vals[idx] = row["load_gib"]
            pod_values[pod] = vals
            max_y = max(max_y, max(vals) if vals else 0.0)

        cols = 2 if pod_count <= 4 else 4
        rows_n = math.ceil(len(pods) / cols)
        fig_w = 11 if cols == 2 else 14
        fig_h = max(3.5, rows_n * 2.7)
        fig, axes = plt.subplots(rows_n, cols, figsize=(fig_w, fig_h), squeeze=False, sharey=True)

        for idx, ax in enumerate(axes.ravel()):
            if idx >= len(pods):
                ax.axis("off")
                continue
            pod = pods[idx]
            vals = pod_values[pod]
            mean = float(np.mean(vals)) if vals else 0.0
            ratio = max(vals) / mean if mean else 0.0
            x = np.arange(len(vals))
            ax.bar(x, vals, color=bar_colors, width=0.82)
            ax.axhline(mean, color="#D62728", linestyle="--", linewidth=1.0)
            ax.set_title(f"{pod_count}pod-pod{pod + 1}  max/mean={ratio:.6f}", fontsize=10)
            ax.set_ylim(0, max_y * 1.12 if max_y else 1.0)
            ax.grid(axis="y", alpha=0.25)
            ax.set_ylabel("GiB")
            ax.set_xticks(x)
            if pod_count <= 4:
                ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
            else:
                ax.set_xticklabels([])

        handles = [
            plt.Rectangle((0, 0), 1, 1, color=palette[p % len(palette)])
            for p in range(cfg.l1_planes)
        ]
        fig.legend(
            handles,
            [f"plane {p}" for p in range(cfg.l1_planes)],
            loc="upper center",
            ncol=cfg.l1_planes,
            bbox_to_anchor=(0.5, 1.0),
        )
        fig.suptitle(f"{label} L1 EPS load by pod, {pod_count} active pods, trial {detail_trial}", y=1.035)
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        fig.savefig(out_dir / f"{kind}_l1_load_by_pod_pods{pod_count}_trial{detail_trial}.png", dpi=180)
        plt.close(fig)


def plot_l0_eps_load_by_pod(
    out_dir: Path,
    pod_counts: list[int],
    detail_trial: int,
    cfg: TopologyCfg,
    kind: str,
    label: str,
) -> None:
    palette = ["#4C78A8", "#72B7B2", "#F58518", "#54A24B"]

    for pod_count in pod_counts:
        csv_path = out_dir / f"{kind}_l0_loads_pods{pod_count}_trial{detail_trial}.csv"
        if not csv_path.exists():
            continue

        by_pod: dict[int, list[dict]] = defaultdict(list)
        with csv_path.open(newline="") as f:
            for row in csv.DictReader(f):
                row["tray"] = int(row["tray"])
                row["group"] = int(row["group"])
                row["plane"] = int(row["plane"])
                row["load_gib"] = float(row["load_gib"])
                by_pod[row["group"]].append(row)

        pods = sorted(by_pod)
        if not pods:
            continue

        pod_rows: dict[int, list[dict]] = {}
        max_y = 0.0
        for pod in pods:
            rows = sorted(by_pod[pod], key=lambda r: (r["tray"], r["plane"]))
            pod_rows[pod] = rows
            if rows:
                max_y = max(max_y, max(row["load_gib"] for row in rows))

        cols = 2 if pod_count <= 4 else 4
        rows_n = math.ceil(len(pods) / cols)
        fig_w = 12 if cols == 2 else 15
        fig_h = max(3.5, rows_n * 2.7)
        fig, axes = plt.subplots(rows_n, cols, figsize=(fig_w, fig_h), squeeze=False, sharey=True)

        for idx, ax in enumerate(axes.ravel()):
            if idx >= len(pods):
                ax.axis("off")
                continue
            pod = pods[idx]
            rows = pod_rows[pod]
            vals = [row["load_gib"] for row in rows]
            mean = float(np.mean(vals)) if vals else 0.0
            ratio = max(vals) / mean if mean else 0.0
            x = np.arange(len(vals))
            colors = [palette[row["plane"] % len(palette)] for row in rows]
            ax.bar(x, vals, color=colors, width=0.9)
            ax.axhline(mean, color="#D62728", linestyle="--", linewidth=1.0)
            ax.set_title(
                f"{pod_count}pod-pod{pod + 1}  L0={len(vals)}  max/mean={ratio:.6f}",
                fontsize=10,
            )
            ax.set_ylim(0, max_y * 1.12 if max_y else 1.0)
            ax.grid(axis="y", alpha=0.25)
            ax.set_ylabel("GiB")
            ax.set_xticks([])

        handles = [
            plt.Rectangle((0, 0), 1, 1, color=palette[p % len(palette)])
            for p in range(cfg.l1_planes)
        ]
        fig.legend(
            handles,
            [f"plane {p}" for p in range(cfg.l1_planes)],
            loc="upper center",
            ncol=cfg.l1_planes,
            bbox_to_anchor=(0.5, 1.0),
        )
        fig.suptitle(f"{label} L0 EPS load by pod, {pod_count} active pods, trial {detail_trial}", y=1.035)
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        fig.savefig(out_dir / f"{kind}_l0_load_by_pod_pods{pod_count}_trial{detail_trial}.png", dpi=180)
        plt.close(fig)


def write_and_plot_l1_pair_matrix(
    out_dir: Path,
    pod_count: int,
    detail_trial: int,
    assignments: list[LptAssignment],
    active_groups: list[int],
    cfg: TopologyCfg,
) -> None:
    l1_ids = [
        l1_id_for_group_plane_eps(g, p, e, cfg)
        for g in active_groups
        for p in range(cfg.l1_planes)
        for e in range(cfg.l1_eps_per_l1_plane)
    ]
    if not l1_ids:
        return

    pos = {l1_id: idx for idx, l1_id in enumerate(l1_ids)}
    matrix = np.zeros((len(l1_ids), len(l1_ids)), dtype=np.float64)
    flow_counts = np.zeros((len(l1_ids), len(l1_ids)), dtype=np.int64)

    for a in assignments:
        if a.src_group == a.dst_group:
            continue
        src_pos = pos.get(a.src_l1_id)
        dst_pos = pos.get(a.dst_l1_id)
        if src_pos is None or dst_pos is None:
            continue
        matrix[src_pos, dst_pos] += a.bytes
        flow_counts[src_pos, dst_pos] += 1

    rows = []
    for src_id, src_idx in pos.items():
        src_group = src_id // (cfg.l1_planes * cfg.l1_eps_per_l1_plane)
        src_plane = (src_id // cfg.l1_eps_per_l1_plane) % cfg.l1_planes
        src_eps = src_id % cfg.l1_eps_per_l1_plane
        for dst_id, dst_idx in pos.items():
            value = matrix[src_idx, dst_idx]
            if value <= 0:
                continue
            dst_group = dst_id // (cfg.l1_planes * cfg.l1_eps_per_l1_plane)
            dst_plane = (dst_id // cfg.l1_eps_per_l1_plane) % cfg.l1_planes
            dst_eps = dst_id % cfg.l1_eps_per_l1_plane
            rows.append({
                "src_l1_id": src_id,
                "src_group": src_group,
                "src_plane": src_plane,
                "src_eps": src_eps,
                "dst_l1_id": dst_id,
                "dst_group": dst_group,
                "dst_plane": dst_plane,
                "dst_eps": dst_eps,
                "flow_count": int(flow_counts[src_idx, dst_idx]),
                "bytes": int(value),
                "gib": f"{gib(value):.9f}",
            })

    write_csv(
        out_dir / f"l1_ocs_crosspod_matrix_pods{pod_count}_trial{detail_trial}.csv",
        [
            "src_l1_id", "src_group", "src_plane", "src_eps",
            "dst_l1_id", "dst_group", "dst_plane", "dst_eps",
            "flow_count", "bytes", "gib",
        ],
        rows,
    )

    matrix_gib = matrix / (1024.0 ** 3)
    masked = np.ma.masked_where(matrix_gib <= 0, matrix_gib)
    cmap = plt.cm.Blues.copy()
    cmap.set_bad(color="white")

    size = max(6.5, min(14.0, len(l1_ids) * 0.055 + 4.0))
    fig, ax = plt.subplots(figsize=(size, size))
    im = ax.imshow(masked, cmap=cmap, interpolation="nearest", aspect="equal")
    ax.set_title(f"L1-to-L1 OCS cross-pod matrix, {pod_count} active pods, trial {detail_trial}")
    ax.set_xlabel("dst L1 EPS")
    ax.set_ylabel("src L1 EPS")
    if len(l1_ids) <= 64:
        labels = [
            f"G{l1_id // (cfg.l1_planes * cfg.l1_eps_per_l1_plane)}"
            f"P{(l1_id // cfg.l1_eps_per_l1_plane) % cfg.l1_planes}"
            f"E{l1_id % cfg.l1_eps_per_l1_plane}"
            for l1_id in l1_ids
        ]
        ticks = np.arange(len(l1_ids))
        ax.set_xticks(ticks)
        ax.set_yticks(ticks)
        ax.set_xticklabels(labels, rotation=90, fontsize=6)
        ax.set_yticklabels(labels, fontsize=6)
    else:
        ax.set_xticks([])
        ax.set_yticks([])
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("GiB")
    nonzero = matrix_gib[matrix_gib > 0]
    if nonzero.size:
        ax.text(
            0.01,
            -0.07,
            f"active pairs={nonzero.size}, max/mean={nonzero.max() / nonzero.mean():.4f}, "
            f"max={nonzero.max():.4f} GiB",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=9,
        )
    fig.tight_layout()
    fig.savefig(out_dir / f"l1_ocs_crosspod_matrix_pods{pod_count}_trial{detail_trial}.png", dpi=180)
    plt.close(fig)


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
            assignments, src_l0_load, dst_l0_load, src_l1_load, dst_l1_load, lpt_cost = lpt_assign(
                placed,
                cfg,
                record_assignments=(trial == args.detail_trial),
            )

            active_src_trays = sorted({rank_tray(r, cfg) for r in placement})
            active_dst_trays = active_src_trays
            active_src_groups = sorted({rank_group(r, cfg) for r in placement})
            active_dst_groups = active_src_groups
            src_l0_active_ids = [l0_id_for_tray_plane(t, p, cfg) for t in active_src_trays for p in range(cfg.l1_planes)]
            dst_l0_active_ids = [l0_id_for_tray_plane(t, p, cfg) for t in active_dst_trays for p in range(cfg.l1_planes)]
            src_l1_active_ids = [
                l1_id_for_group_plane_eps(g, p, e, cfg)
                for g in active_src_groups
                for p in range(cfg.l1_planes)
                for e in range(cfg.l1_eps_per_l1_plane)
            ]
            dst_l1_active_ids = [
                l1_id_for_group_plane_eps(g, p, e, cfg)
                for g in active_dst_groups
                for p in range(cfg.l1_planes)
                for e in range(cfg.l1_eps_per_l1_plane)
            ]
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
                [
                    [l0_id_for_tray_plane(t, p, cfg) for p in range(cfg.l1_planes)]
                    for t in active_src_trays
                ],
            )
            dst_l0_tray_balance = candidate_set_imbalance(
                dst_l0_load,
                [
                    [l0_id_for_tray_plane(t, p, cfg) for p in range(cfg.l1_planes)]
                    for t in active_dst_trays
                ],
            )
            src_l1_group_plane_balance = candidate_set_imbalance(
                src_l1_load,
                [
                    [
                        l1_id_for_group_plane_eps(g, p, e, cfg)
                        for e in range(cfg.l1_eps_per_l1_plane)
                    ]
                    for g in active_src_groups
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
                    for g in active_src_groups
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
                    for g in active_dst_groups
                ],
            )

            # Lower bound if each active L0/L1 resource could be perfectly packed; useful sanity check.
            row = {
                "pod_count": pod_count,
                "trial": trial,
                "active_groups": len(active_src_groups),
                "active_trays": len(active_src_trays),
                "active_src_l0_eps": len(src_l0_active_ids),
                "active_dst_l0_eps": len(dst_l0_active_ids),
                "active_src_l1_eps": len(src_l1_active_ids),
                "active_dst_l1_eps": len(dst_l1_active_ids),
                "flow_count": len(logical_flows),
                "lpt_sort_ms": f"{lpt_cost['sort_ms']:.6f}",
                "lpt_greedy_ms": f"{lpt_cost['greedy_ms']:.6f}",
                "lpt_total_ms": f"{lpt_cost['total_ms']:.6f}",
                "lpt_total_us_per_flow": f"{(lpt_cost['total_ms'] * 1000.0 / len(logical_flows)):.6f}",
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
                write_csv(
                    out_dir / f"src_l0_loads_pods{pod_count}_trial{trial}.csv",
                    ["l0_id", "tray", "group", "plane", "load_bytes", "load_gib"],
                    [
                        {
                            "l0_id": x,
                            "tray": x // cfg.l1_planes,
                            "group": tray_group(x // cfg.l1_planes, cfg),
                            "plane": x % cfg.l1_planes,
                            "load_bytes": src_l0_load.get(x, 0),
                            "load_gib": f"{gib(src_l0_load.get(x, 0)):.9f}",
                        }
                        for x in src_l0_active_ids
                    ],
                )
                write_csv(
                    out_dir / f"dst_l0_loads_pods{pod_count}_trial{trial}.csv",
                    ["l0_id", "tray", "group", "plane", "load_bytes", "load_gib"],
                    [
                        {
                            "l0_id": x,
                            "tray": x // cfg.l1_planes,
                            "group": tray_group(x // cfg.l1_planes, cfg),
                            "plane": x % cfg.l1_planes,
                            "load_bytes": dst_l0_load.get(x, 0),
                            "load_gib": f"{gib(dst_l0_load.get(x, 0)):.9f}",
                        }
                        for x in dst_l0_active_ids
                    ],
                )
                write_csv(
                    out_dir / f"src_l1_loads_pods{pod_count}_trial{trial}.csv",
                    ["l1_id", "group", "plane", "eps", "load_bytes", "load_gib"],
                    [
                        {
                            "l1_id": x,
                            "group": x // (cfg.l1_planes * cfg.l1_eps_per_l1_plane),
                            "plane": (x // cfg.l1_eps_per_l1_plane) % cfg.l1_planes,
                            "eps": x % cfg.l1_eps_per_l1_plane,
                            "load_bytes": src_l1_load.get(x, 0),
                            "load_gib": f"{gib(src_l1_load.get(x, 0)):.9f}",
                        }
                        for x in src_l1_active_ids
                    ],
                )
                write_csv(
                    out_dir / f"dst_l1_loads_pods{pod_count}_trial{trial}.csv",
                    ["l1_id", "group", "plane", "eps", "load_bytes", "load_gib"],
                    [
                        {
                            "l1_id": x,
                            "group": x // (cfg.l1_planes * cfg.l1_eps_per_l1_plane),
                            "plane": (x // cfg.l1_eps_per_l1_plane) % cfg.l1_planes,
                            "eps": x % cfg.l1_eps_per_l1_plane,
                            "load_bytes": dst_l1_load.get(x, 0),
                            "load_gib": f"{gib(dst_l1_load.get(x, 0)):.9f}",
                        }
                        for x in dst_l1_active_ids
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
                write_and_plot_l1_pair_matrix(out_dir, pod_count, trial, assignments, active_src_groups, cfg)
                # Pod traffic totals from placement and logical flows.
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
        "flow_count", "lpt_sort_ms", "lpt_greedy_ms", "lpt_total_ms", "lpt_total_us_per_flow", "total_gib",
        "src_l0_mean_gib", "src_l0_p95_gib", "src_l0_max_gib", "src_l0_std_gib",
        "src_l0_imbalance_max_mean", "src_l0_in_tray_imbalance_mean",
        "src_l0_in_tray_imbalance_p95", "src_l0_in_tray_imbalance_max",
        "dst_l0_mean_gib", "dst_l0_p95_gib", "dst_l0_max_gib", "dst_l0_std_gib",
        "dst_l0_imbalance_max_mean", "dst_l0_in_tray_imbalance_mean",
        "dst_l0_in_tray_imbalance_p95", "dst_l0_in_tray_imbalance_max",
        "src_l1_mean_gib", "src_l1_p95_gib", "src_l1_max_gib", "src_l1_std_gib",
        "src_l1_imbalance_max_mean", "src_l1_in_pod_imbalance_mean", "src_l1_in_pod_imbalance_p95",
        "src_l1_in_pod_imbalance_max", "src_l1_in_group_plane_imbalance_mean",
        "src_l1_in_group_plane_imbalance_p95", "src_l1_in_group_plane_imbalance_max",
        "dst_l1_mean_gib", "dst_l1_p95_gib", "dst_l1_max_gib", "dst_l1_std_gib",
        "dst_l1_imbalance_max_mean", "dst_l1_in_pod_imbalance_mean",
        "dst_l1_in_pod_imbalance_p95", "dst_l1_in_pod_imbalance_max",
    ]
    write_csv(out_dir / "lpt_summary_by_trial.csv", fields, summary_rows)

    # Aggregated summary by pod count.
    agg_rows = []
    for pod_count in args.pod_counts:
        rows = [r for r in summary_rows if int(r["pod_count"]) == pod_count]
        for metric in [
            "src_l0_imbalance_max_mean",
            "dst_l0_imbalance_max_mean",
            "src_l1_imbalance_max_mean",
            "src_l1_in_pod_imbalance_mean",
            "src_l1_in_pod_imbalance_p95",
            "src_l1_in_pod_imbalance_max",
            "dst_l1_imbalance_max_mean",
            "dst_l1_in_pod_imbalance_mean",
            "dst_l1_in_pod_imbalance_p95",
            "dst_l1_in_pod_imbalance_max",
            "src_l0_in_tray_imbalance_mean",
            "src_l0_in_tray_imbalance_p95",
            "src_l0_in_tray_imbalance_max",
            "dst_l0_in_tray_imbalance_mean",
            "dst_l0_in_tray_imbalance_p95",
            "dst_l0_in_tray_imbalance_max",
            "src_l1_in_group_plane_imbalance_mean",
            "src_l1_in_group_plane_imbalance_p95",
            "src_l1_in_group_plane_imbalance_max",
            "src_l0_max_gib",
            "dst_l0_max_gib",
            "src_l1_max_gib",
            "dst_l1_max_gib",
            "src_l0_p95_gib",
            "dst_l0_p95_gib",
            "src_l1_p95_gib",
            "dst_l1_p95_gib",
            "lpt_sort_ms",
            "lpt_greedy_ms",
            "lpt_total_ms",
            "lpt_total_us_per_flow",
        ]:
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

    readme = f"""# LPT TE 理论建模结果

输入流量来自 `{args.distribution_csv}`，按 EP{args.ep_ranks}、`tokens_per_rank={args.tokens_per_rank}`、`topk={args.topk}`、`hidden_size={args.hidden_size}`、`dtype_bytes={args.dtype_bytes}` 生成单层 dispatch-only all-to-all。

总流量：{gib(total_bytes):.3f} GiB，flow 数：{len(logical_flows)}。

拓扑参数：`nodes={cfg.nodes}`、`groups={cfg.groups}`、`ranks_per_group={cfg.ranks_per_group}`、`ranks_per_tray={cfg.ranks_per_tray}`、`l1_planes={cfg.l1_planes}`、`l1_eps_per_l1_plane={cfg.l1_eps_per_l1_plane}`。

LPT 规则：

1. flow 按大小从大到小排序。
2. 对同一条 flow，一次性同时决定源侧和目的侧。
3. 源侧：在源 tray 的 `{cfg.l1_planes}` 个 L0 plane 中选择当前发送负载最小的 L0 EPS，然后在源 group、同 plane 的 `{cfg.l1_eps_per_l1_plane}` 个 L1 EPS 中选择当前发送负载最小的 L1 EPS。
4. 目的侧：在目的 tray 的 `{cfg.l1_planes}` 个 L0 plane 中选择当前接收负载最小的 L0 EPS，然后在目的 group 的 `{cfg.l1_planes * cfg.l1_eps_per_l1_plane}` 个 L1 EPS 中选择当前接收负载最小的 L1 EPS。
5. 每条 flow 是 single path，不做 packet spray；OCS 视为源 L1 到目的 L1 的直连管道。

输出文件：

- `lpt_summary_by_trial.csv`：每个 pod 数、每个随机 placement trial 的源/目的 L0、源/目的 L1 负载统计。
- `lpt_summary_aggregate.csv`：按 pod 数聚合后的均值、最小值、最大值。
- `src_l0_loads_pods*_trial0.csv`：detail trial 的源 L0 EPS 发送负载。
- `src_l1_loads_pods*_trial0.csv`：detail trial 的源 L1 EPS 发送负载。
- `dst_l0_loads_pods*_trial0.csv`：detail trial 的目的 L0 EPS 接收负载。
- `dst_l1_loads_pods*_trial0.csv`：detail trial 的目的 L1 EPS 接收负载。
- `assignments_pods*_trial0.csv`：detail trial 的 flow -> 源 L0 / 源 L1 / 目的 L0 / 目的 L1 决策。
- `l1_ocs_crosspod_matrix_pods*_trial0.csv/png`：跨 pod 的源 L1 到目的 L1 负载矩阵。
- `traffic_by_pod_detail_trial.csv`：detail trial 的 pod 级收发总量。
- `lpt_summary_by_pod_count.png`：不同 pod 数下的 LPT 残余不均衡。
- `lpt_eps_load_cdf_detail_trial.png`：detail trial 的源/目的 L0、源/目的 L1 EPS 负载 CDF。
- `traffic_by_pod_detail_trial.png`：detail trial 的 pod 收发总量。
- `src_l0_load_by_pod_pods*_trial0.png`：每个 pod 内源 L0 EPS 的发送负载柱状图。
- `src_l1_load_by_pod_pods*_trial0.png`：每个 pod 内源 L1 EPS 的发送负载柱状图。
- `dst_l0_load_by_pod_pods*_trial0.png`：每个 pod 内目的 L0 EPS 的接收负载柱状图。
- `dst_l1_load_by_pod_pods*_trial0.png`：每个 pod 内目的 L1 EPS 的接收负载柱状图。
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")

    print(f"wrote {out_dir}")
    print(f"flows {len(logical_flows)} total_gib {gib(total_bytes):.3f}")
    for pod_count in args.pod_counts:
        rows = [r for r in summary_rows if int(r["pod_count"]) == pod_count]
        src_l0 = np.mean([float(r["src_l0_imbalance_max_mean"]) for r in rows])
        dst_l0 = np.mean([float(r["dst_l0_imbalance_max_mean"]) for r in rows])
        src_l1 = np.mean([float(r["src_l1_imbalance_max_mean"]) for r in rows])
        dst_l1 = np.mean([float(r["dst_l1_imbalance_max_mean"]) for r in rows])
        print(
            f"pods {pod_count}: avg src L0 {src_l0:.4f}, src L1 {src_l1:.4f}, "
            f"dst L0 {dst_l0:.4f}, dst L1 {dst_l1:.4f}"
        )


if __name__ == "__main__":
    main()
