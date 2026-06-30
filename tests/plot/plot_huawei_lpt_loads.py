#!/usr/bin/env python3
"""Plot LPT source-route EPS and OCS load from a Huawei route plan."""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def parse_run_config(run_dir: Path) -> dict[str, int]:
    cfg = {
        "groups": 16,
        "l1_planes": 4,
        "l1_eps_per_l1_plane": 4,
    }
    path = run_dir / "run_config.txt"
    if not path.exists():
        return cfg
    patterns = {
        "groups": re.compile(r"^\s*groups:\s*(\d+)\s*$"),
        "l1_planes": re.compile(r"^\s*l1_planes:\s*(\d+)\s*$"),
        "l1_eps_per_l1_plane": re.compile(r"^\s*l1_eps_per_l1_plane:\s*(\d+)\s*$"),
    }
    for line in path.read_text().splitlines():
        for key, pattern in patterns.items():
            m = pattern.match(line)
            if m:
                cfg[key] = int(m.group(1))
    return cfg


def l1_group(l1_id: int, l1_planes: int, l1_eps_per_plane: int) -> int:
    return l1_id // (l1_planes * l1_eps_per_plane)


def l1_label(l1_id: int, l1_planes: int, l1_eps_per_plane: int) -> str:
    group = l1_group(l1_id, l1_planes, l1_eps_per_plane)
    local = l1_id % (l1_planes * l1_eps_per_plane)
    plane = local // l1_eps_per_plane
    eps = local % l1_eps_per_plane
    return f"g{group}p{plane}e{eps}"


def write_metric_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_stats(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {k: 0.0 for k in ["count", "sum_mib", "mean_mib", "min_mib", "p50_mib", "p95_mib", "max_mib", "max_over_mean", "cv"]}
    mean = float(np.mean(values))
    std = float(np.std(values))
    return {
        "count": float(values.size),
        "sum_mib": float(np.sum(values)),
        "mean_mib": mean,
        "min_mib": float(np.min(values)),
        "p50_mib": float(np.percentile(values, 50)),
        "p95_mib": float(np.percentile(values, 95)),
        "max_mib": float(np.max(values)),
        "max_over_mean": float(np.max(values) / mean) if mean > 0 else 0.0,
        "cv": float(std / mean) if mean > 0 else 0.0,
    }


def plot_eps_loads(
    src_mib: np.ndarray,
    dst_mib: np.ndarray,
    out: Path,
    groups: int,
    l1_per_group: int,
) -> None:
    x = np.arange(len(src_mib))
    ymax = max(float(np.max(src_mib)) if src_mib.size else 0.0,
               float(np.max(dst_mib)) if dst_mib.size else 0.0)
    ymax = ymax * 1.08 if ymax > 0 else 1.0

    fig, axes = plt.subplots(2, 1, figsize=(18, 8), sharex=True)
    for ax, values, title, color in [
        (axes[0], src_mib, "src L1-EPS egress load from LPT route plan", "#2b6cb0"),
        (axes[1], dst_mib, "dst L1-EPS ingress load from LPT route plan", "#2f855a"),
    ]:
        ax.bar(x, values, width=0.85, color=color, alpha=0.88)
        ax.set_ylabel("MiB")
        ax.set_ylim(0, ymax)
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.25)
        for g in range(1, groups):
            ax.axvline(g * l1_per_group - 0.5, color="black", lw=0.4, alpha=0.25)

    tick_positions = [g * l1_per_group + (l1_per_group - 1) / 2 for g in range(groups)]
    axes[1].set_xticks(tick_positions)
    axes[1].set_xticklabels([f"g{g}" for g in range(groups)], rotation=0)
    axes[1].set_xlabel("L1-EPS groups, each group has plane*eps switches")
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def plot_ocs_matrix(matrix_mib: np.ndarray, out: Path, groups: int, l1_per_group: int) -> None:
    fig, ax = plt.subplots(figsize=(10, 9))
    masked = np.ma.masked_where(matrix_mib <= 0, matrix_mib)
    cmap = plt.cm.Blues.copy()
    cmap.set_bad(color="white")
    im = ax.imshow(masked, cmap=cmap, interpolation="nearest", aspect="auto")
    ax.set_title("OCS load matrix: src L1-EPS -> dst L1-EPS")
    ax.set_xlabel("dst L1-EPS")
    ax.set_ylabel("src L1-EPS")
    ticks = [g * l1_per_group + (l1_per_group - 1) / 2 for g in range(groups)]
    labels = [f"g{g}" for g in range(groups)]
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=90)
    ax.set_yticks(ticks)
    ax.set_yticklabels(labels)
    for g in range(1, groups):
        ax.axvline(g * l1_per_group - 0.5, color="black", lw=0.3, alpha=0.25)
        ax.axhline(g * l1_per_group - 0.5, color="black", lw=0.3, alpha=0.25)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("MiB")
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def group_stats(values: np.ndarray, groups: int, l1_per_group: int, scope: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for group in range(groups):
        start = group * l1_per_group
        group_values = values[start:start + l1_per_group]
        stats = load_stats(group_values)
        row: dict[str, object] = {"scope": scope, "group": group}
        row.update(stats)
        rows.append(row)
    return rows


def plot_group_imbalance(group_rows: list[dict[str, object]], out: Path) -> None:
    src = [row for row in group_rows if row["scope"] == "src_l1_eps"]
    dst = [row for row in group_rows if row["scope"] == "dst_l1_eps"]
    x = np.arange(len(src))
    width = 0.38
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)

    axes[0].bar(x - width / 2, [float(row["max_over_mean"]) for row in src],
                width=width, label="src", color="#2b6cb0", alpha=0.88)
    axes[0].bar(x + width / 2, [float(row["max_over_mean"]) for row in dst],
                width=width, label="dst", color="#2f855a", alpha=0.88)
    axes[0].set_ylabel("max / mean")
    axes[0].set_title("Per-pod L1-EPS imbalance")
    axes[0].grid(axis="y", alpha=0.25)
    axes[0].legend()

    axes[1].bar(x - width / 2, [float(row["sum_mib"]) for row in src],
                width=width, label="src", color="#2b6cb0", alpha=0.88)
    axes[1].bar(x + width / 2, [float(row["sum_mib"]) for row in dst],
                width=width, label="dst", color="#2f855a", alpha=0.88)
    axes[1].set_ylabel("MiB")
    axes[1].set_xlabel("pod/group")
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([f"g{int(row['group'])}" for row in src], rotation=0)
    axes[1].set_title("Per-pod total L1-EPS load")
    axes[1].grid(axis="y", alpha=0.25)
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--route-plan", type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir
    cfg = parse_run_config(run_dir)
    groups = cfg["groups"]
    l1_planes = cfg["l1_planes"]
    l1_eps_per_plane = cfg["l1_eps_per_l1_plane"]
    l1_per_group = l1_planes * l1_eps_per_plane
    l1_count = groups * l1_per_group

    route_plan = args.route_plan or (run_dir / "data" / "huawei_route_plan_lpt.csv")
    out_dir = args.out_dir or (run_dir / "output_metrics" / "lpt_theory_load")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = read_csv(route_plan)
    src_l1_bytes = np.zeros(l1_count, dtype=np.float64)
    dst_l1_bytes = np.zeros(l1_count, dtype=np.float64)
    ocs_matrix_bytes = np.zeros((l1_count, l1_count), dtype=np.float64)
    ocs_pair_rows: list[dict[str, object]] = []

    for row in rows:
        size = int(row["bytes"])
        src_l1 = int(row["src_l1_id"])
        dst_l1 = int(row["dst_l1_id"])
        if 0 <= src_l1 < l1_count:
            src_l1_bytes[src_l1] += size
        if 0 <= dst_l1 < l1_count:
            dst_l1_bytes[dst_l1] += size
        if 0 <= src_l1 < l1_count and 0 <= dst_l1 < l1_count:
            if l1_group(src_l1, l1_planes, l1_eps_per_plane) != l1_group(dst_l1, l1_planes, l1_eps_per_plane):
                ocs_matrix_bytes[src_l1, dst_l1] += size

    src_mib = src_l1_bytes / (1024 * 1024)
    dst_mib = dst_l1_bytes / (1024 * 1024)
    ocs_matrix_mib = ocs_matrix_bytes / (1024 * 1024)

    src_rows = [
        {
            "l1_id": i,
            "label": l1_label(i, l1_planes, l1_eps_per_plane),
            "group": l1_group(i, l1_planes, l1_eps_per_plane),
            "bytes": int(src_l1_bytes[i]),
            "mib": src_mib[i],
        }
        for i in range(l1_count)
    ]
    dst_rows = [
        {
            "l1_id": i,
            "label": l1_label(i, l1_planes, l1_eps_per_plane),
            "group": l1_group(i, l1_planes, l1_eps_per_plane),
            "bytes": int(dst_l1_bytes[i]),
            "mib": dst_mib[i],
        }
        for i in range(l1_count)
    ]

    for src_l1 in range(l1_count):
        for dst_l1 in range(l1_count):
            load = int(ocs_matrix_bytes[src_l1, dst_l1])
            if load > 0:
                ocs_pair_rows.append(
                    {
                        "src_l1_id": src_l1,
                        "dst_l1_id": dst_l1,
                        "src_label": l1_label(src_l1, l1_planes, l1_eps_per_plane),
                        "dst_label": l1_label(dst_l1, l1_planes, l1_eps_per_plane),
                        "bytes": load,
                        "mib": load / (1024 * 1024),
                    }
                )

    write_metric_csv(out_dir / "src_l1_eps_load.csv", src_rows, ["l1_id", "label", "group", "bytes", "mib"])
    write_metric_csv(out_dir / "dst_l1_eps_load.csv", dst_rows, ["l1_id", "label", "group", "bytes", "mib"])
    write_metric_csv(
        out_dir / "ocs_l1_pair_load.csv",
        ocs_pair_rows,
        ["src_l1_id", "dst_l1_id", "src_label", "dst_label", "bytes", "mib"],
    )

    summary_rows = []
    for name, values in [
        ("src_l1_eps_all", src_mib),
        ("dst_l1_eps_all", dst_mib),
        ("src_l1_eps_active", src_mib[src_mib > 0]),
        ("dst_l1_eps_active", dst_mib[dst_mib > 0]),
        ("ocs_l1_pair_active", ocs_matrix_mib[ocs_matrix_mib > 0]),
    ]:
        stats = load_stats(values)
        for metric, value in stats.items():
            summary_rows.append({"scope": name, "metric": metric, "value": value})
    write_metric_csv(out_dir / "lpt_load_summary.csv", summary_rows, ["scope", "metric", "value"])

    group_rows = group_stats(src_mib, groups, l1_per_group, "src_l1_eps")
    group_rows.extend(group_stats(dst_mib, groups, l1_per_group, "dst_l1_eps"))
    write_metric_csv(
        out_dir / "l1_eps_group_summary.csv",
        group_rows,
        ["scope", "group", "count", "sum_mib", "mean_mib", "min_mib", "p50_mib",
         "p95_mib", "max_mib", "max_over_mean", "cv"],
    )

    plot_eps_loads(src_mib, dst_mib, out_dir / "lpt_l1_eps_load.png", groups, l1_per_group)
    plot_ocs_matrix(ocs_matrix_mib, out_dir / "lpt_ocs_l1_matrix.png", groups, l1_per_group)
    plot_group_imbalance(group_rows, out_dir / "lpt_l1_eps_group_imbalance.png")

    with (out_dir / "README.txt").open("w") as f:
        f.write("LPT theoretical route-plan load analysis\n")
        f.write("========================================\n")
        f.write(f"route_plan: {route_plan}\n")
        f.write(f"l1_count: {l1_count}\n")
        f.write("source: route plan payload bytes only; this does not include retransmissions, RTS/ACK, ECN, or sampled link bytes\n")
        f.write("src_l1_eps_load.csv: per L1-EPS egress bytes from source-side LPT assignment\n")
        f.write("dst_l1_eps_load.csv: per L1-EPS ingress bytes from destination-side LPT assignment\n")
        f.write("ocs_l1_pair_load.csv: nonzero cross-group src L1 -> dst L1 planned OCS bytes\n")
        f.write("lpt_load_summary.csv: imbalance statistics in MiB\n")
        f.write("l1_eps_group_summary.csv: per-pod L1-EPS imbalance statistics\n")

    print(f"Wrote LPT load analysis to {out_dir}")
    print(f"  {out_dir / 'lpt_l1_eps_load.png'}")
    print(f"  {out_dir / 'lpt_ocs_l1_matrix.png'}")


if __name__ == "__main__":
    main()
