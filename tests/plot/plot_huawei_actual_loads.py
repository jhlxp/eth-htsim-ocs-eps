#!/usr/bin/env python3
"""Plot actual Huawei EPS and OCS load from HTSIM link-load samples."""

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


GIB = 1024.0 ** 3
PALETTE = ["#4C78A8", "#72B7B2", "#F58518", "#54A24B"]
MEAN_COLOR = "#D62728"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_run_config(run_dir: Path) -> dict[str, str]:
    cfg = {
        "groups": "16",
        "ranks_per_group": "512",
        "ranks_per_tray": "8",
        "l1_planes": "4",
        "l1_eps_per_l1_plane": "4",
        "route_plan_algo": "none",
        "huawei_ocs_mode": "unknown",
        "huawei_ocs_choice": "unknown",
        "switch_strategy": "unknown",
    }
    path = run_dir / "run_config.txt"
    if not path.exists():
        return cfg
    pattern = re.compile(r"^\s*([A-Za-z0-9_]+):\s*(.*?)\s*$")
    for line in path.read_text().splitlines():
        match = pattern.match(line)
        if match and match.group(1) in cfg:
            cfg[match.group(1)] = match.group(2)
    return cfg


def cfg_int(cfg: dict[str, str], key: str) -> int:
    return int(cfg[key])


def infer_algo_name(cfg: dict[str, str]) -> tuple[str, str]:
    route_algo = cfg.get("route_plan_algo", "none")
    ocs_mode = cfg.get("huawei_ocs_mode", "unknown")
    if route_algo == "lpt":
        return "lpt", "LPT source route"
    if route_algo == "ecmp":
        return "ecmp", "Single-flow ECMP"
    return "spray", f"Packet spray ({ocs_mode})"


def aggregate_link_bytes(load_path: Path) -> dict[int, int]:
    totals: dict[int, int] = defaultdict(int)
    with load_path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            totals[int(row["link_id"])] += int(row["bytes"])
    return totals


def l1_parts(l1_id: int, l1_planes: int, l1_eps_per_plane: int) -> tuple[int, int, int]:
    l1_per_group = l1_planes * l1_eps_per_plane
    group = l1_id // l1_per_group
    local = l1_id % l1_per_group
    return group, local // l1_eps_per_plane, local % l1_eps_per_plane


def l1_label(l1_id: int, l1_planes: int, l1_eps_per_plane: int) -> str:
    group, plane, eps = l1_parts(l1_id, l1_planes, l1_eps_per_plane)
    return f"G{group}P{plane}E{eps}"


def l0_parts(l0_id: int, ranks_per_group: int, ranks_per_tray: int, l1_planes: int) -> tuple[int, int, int]:
    trays_per_group = ranks_per_group // ranks_per_tray
    l0_per_group = trays_per_group * l1_planes
    group = l0_id // l0_per_group
    local = l0_id % l0_per_group
    tray = local // l1_planes
    plane = local % l1_planes
    return group, tray, plane


def stats(values: np.ndarray) -> dict[str, float]:
    active = values[values > 0]
    base = active if active.size else values
    if base.size == 0:
        return {
            "count": 0.0,
            "active": 0.0,
            "sum_gib": 0.0,
            "mean_gib": 0.0,
            "p50_gib": 0.0,
            "p95_gib": 0.0,
            "max_gib": 0.0,
            "max_over_mean": 0.0,
            "cv": 0.0,
        }
    mean = float(np.mean(base))
    max_v = float(np.max(base))
    return {
        "count": float(values.size),
        "active": float(active.size),
        "sum_gib": float(np.sum(values)),
        "mean_gib": mean,
        "p50_gib": float(np.percentile(base, 50)),
        "p95_gib": float(np.percentile(base, 95)),
        "max_gib": max_v,
        "max_over_mean": max_v / mean if mean > 0 else 0.0,
        "cv": float(np.std(base) / mean) if mean > 0 else 0.0,
    }


def active_groups(values_by_group: dict[int, list[float]], groups: int) -> list[int]:
    active = [g for g in range(groups) if sum(values_by_group.get(g, [])) > 0]
    return active if active else list(range(groups))


def plot_l1_by_pod(
    values: np.ndarray,
    out: Path,
    title: str,
    groups: int,
    l1_planes: int,
    l1_eps_per_plane: int,
) -> None:
    l1_per_group = l1_planes * l1_eps_per_plane
    labels = [f"P{p}E{e}" for p in range(l1_planes) for e in range(l1_eps_per_plane)]
    bar_colors = [
        PALETTE[plane % len(PALETTE)]
        for plane in range(l1_planes)
        for _ in range(l1_eps_per_plane)
    ]
    by_group = {
        group: values[group * l1_per_group:(group + 1) * l1_per_group].tolist()
        for group in range(groups)
    }
    pods = active_groups(by_group, groups)
    max_y = max((max(by_group[g]) for g in pods if by_group[g]), default=0.0)

    cols = 2 if len(pods) <= 4 else 4
    rows_n = math.ceil(len(pods) / cols)
    fig_w = 11 if cols == 2 else 14
    fig_h = max(3.5, rows_n * 2.7)
    fig, axes = plt.subplots(rows_n, cols, figsize=(fig_w, fig_h), squeeze=False, sharey=True)

    for idx, ax in enumerate(axes.ravel()):
        if idx >= len(pods):
            ax.axis("off")
            continue
        pod = pods[idx]
        vals = by_group[pod]
        active = [v for v in vals if v > 0]
        mean = float(np.mean(active)) if active else 0.0
        ratio = (max(active) / mean) if mean else 0.0
        x = np.arange(len(vals))
        ax.bar(x, vals, color=bar_colors, width=0.82)
        if mean > 0:
            ax.axhline(mean, color=MEAN_COLOR, linestyle="--", linewidth=1.0)
        ax.set_title(
            f"pod{pod + 1}  active={len(active)}/{len(vals)}  max/mean={ratio:.4f}",
            fontsize=10,
        )
        ax.set_ylim(0, max_y * 1.12 if max_y else 1.0)
        ax.grid(axis="y", alpha=0.25)
        ax.set_ylabel("GiB")
        ax.set_xticks(x)
        if len(pods) <= 4:
            ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
        else:
            ax.set_xticklabels([])

    handles = [plt.Rectangle((0, 0), 1, 1, color=PALETTE[p % len(PALETTE)]) for p in range(l1_planes)]
    fig.legend(
        handles,
        [f"plane {p}" for p in range(l1_planes)],
        loc="upper center",
        ncol=l1_planes,
        bbox_to_anchor=(0.5, 0.965),
    )
    fig.suptitle(title, y=0.995, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out, dpi=180)
    plt.close(fig)


def plot_l0_by_pod(
    values: np.ndarray,
    out: Path,
    title: str,
    groups: int,
    ranks_per_group: int,
    ranks_per_tray: int,
    l1_planes: int,
) -> None:
    trays_per_group = ranks_per_group // ranks_per_tray
    l0_per_group = trays_per_group * l1_planes
    by_group = {
        group: values[group * l0_per_group:(group + 1) * l0_per_group].tolist()
        for group in range(groups)
    }
    pods = active_groups(by_group, groups)
    max_y = max((max(by_group[g]) for g in pods if by_group[g]), default=0.0)

    cols = 2 if len(pods) <= 4 else 4
    rows_n = math.ceil(len(pods) / cols)
    fig_w = 12 if cols == 2 else 15
    fig_h = max(3.5, rows_n * 2.7)
    fig, axes = plt.subplots(rows_n, cols, figsize=(fig_w, fig_h), squeeze=False, sharey=True)

    colors = [PALETTE[i % l1_planes % len(PALETTE)] for i in range(l0_per_group)]
    for idx, ax in enumerate(axes.ravel()):
        if idx >= len(pods):
            ax.axis("off")
            continue
        pod = pods[idx]
        vals = by_group[pod]
        active = [v for v in vals if v > 0]
        mean = float(np.mean(active)) if active else 0.0
        ratio = (max(active) / mean) if mean else 0.0
        x = np.arange(len(vals))
        ax.bar(x, vals, color=colors[:len(vals)], width=0.9)
        if mean > 0:
            ax.axhline(mean, color=MEAN_COLOR, linestyle="--", linewidth=1.0)
        ax.set_title(
            f"pod{pod + 1}  active={len(active)}/{len(vals)}  max/mean={ratio:.4f}",
            fontsize=10,
        )
        ax.set_ylim(0, max_y * 1.12 if max_y else 1.0)
        ax.grid(axis="y", alpha=0.25)
        ax.set_ylabel("GiB")
        ax.set_xticks([])

    handles = [plt.Rectangle((0, 0), 1, 1, color=PALETTE[p % len(PALETTE)]) for p in range(l1_planes)]
    fig.legend(
        handles,
        [f"plane {p}" for p in range(l1_planes)],
        loc="upper center",
        ncol=l1_planes,
        bbox_to_anchor=(0.5, 0.965),
    )
    fig.suptitle(title, y=0.995, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(out, dpi=180)
    plt.close(fig)


def plot_ocs_matrix(matrix_gib: np.ndarray, out: Path, title: str, groups: int, l1_per_group: int) -> None:
    masked = np.ma.masked_where(matrix_gib <= 0, matrix_gib)
    cmap = plt.cm.Blues.copy()
    cmap.set_bad(color="white")

    fig, ax = plt.subplots(figsize=(12, 10.5))
    im = ax.imshow(masked, cmap=cmap, interpolation="nearest", aspect="equal")
    ax.set_title(title)
    ax.set_xlabel("dst L1 EPS")
    ax.set_ylabel("src L1 EPS")
    if matrix_gib.shape[0] <= 64:
        ticks = np.arange(matrix_gib.shape[0])
        ax.set_xticks(ticks)
        ax.set_yticks(ticks)
    else:
        ax.set_xticks([])
        ax.set_yticks([])
    for group in range(1, groups):
        pos = group * l1_per_group - 0.5
        ax.axvline(pos, color="black", lw=0.25, alpha=0.22)
        ax.axhline(pos, color="black", lw=0.25, alpha=0.22)
    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("GiB")
    nonzero = matrix_gib[matrix_gib > 0]
    if nonzero.size:
        ax.text(
            0.01,
            -0.06,
            f"active pairs={nonzero.size}, max/mean={nonzero.max() / nonzero.mean():.4f}, "
            f"max={nonzero.max():.4f} GiB",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=9,
        )
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir
    metrics_dir = run_dir / "output_metrics"
    link_info_path = metrics_dir / "link_info.csv"
    link_load_path = metrics_dir / "link_load_1ms.csv"
    if not link_info_path.exists() or not link_load_path.exists():
        raise FileNotFoundError("Need output_metrics/link_info.csv and link_load_1ms.csv")

    cfg = parse_run_config(run_dir)
    algo_slug, algo_label = infer_algo_name(cfg)
    out_dir = args.out_dir or (metrics_dir / f"{algo_slug}_actual_load")
    out_dir.mkdir(parents=True, exist_ok=True)

    groups = cfg_int(cfg, "groups")
    ranks_per_group = cfg_int(cfg, "ranks_per_group")
    ranks_per_tray = cfg_int(cfg, "ranks_per_tray")
    l1_planes = cfg_int(cfg, "l1_planes")
    l1_eps_per_plane = cfg_int(cfg, "l1_eps_per_l1_plane")
    l1_per_group = l1_planes * l1_eps_per_plane
    l1_count = groups * l1_per_group

    info_rows = read_csv(link_info_path)
    link_bytes = aggregate_link_bytes(link_load_path)

    trays_per_group = ranks_per_group // ranks_per_tray
    expected_l0 = groups * trays_per_group * l1_planes
    max_l0 = expected_l0
    for row in info_rows:
        if row["src_type"] == "huawei_l0":
            max_l0 = max(max_l0, int(row["src_id"]) + 1)
        if row["dst_type"] == "huawei_l0":
            max_l0 = max(max_l0, int(row["dst_id"]) + 1)

    l0_host_up = np.zeros(max_l0, dtype=np.float64)
    l0_to_l1 = np.zeros(max_l0, dtype=np.float64)
    l0_from_l1 = np.zeros(max_l0, dtype=np.float64)
    l0_host_down = np.zeros(max_l0, dtype=np.float64)
    l1_from_l0 = np.zeros(l1_count, dtype=np.float64)
    l1_to_ocs = np.zeros(l1_count, dtype=np.float64)
    l1_from_ocs = np.zeros(l1_count, dtype=np.float64)
    l1_to_l0 = np.zeros(l1_count, dtype=np.float64)
    ocs_matrix = np.zeros((l1_count, l1_count), dtype=np.float64)

    for row in info_rows:
        link_id = int(row["link_id"])
        gib = link_bytes.get(link_id, 0) / GIB
        if gib <= 0:
            continue
        layer = row["layer"]
        direction = row["direction"]
        src_type = row["src_type"]
        dst_type = row["dst_type"]
        src_id = int(row["src_id"])
        dst_id = int(row["dst_id"])

        if layer == "huawei_host_l0":
            if direction == "up" and dst_type == "huawei_l0":
                l0_host_up[dst_id] += gib
            elif direction == "down" and src_type == "huawei_l0":
                l0_host_down[src_id] += gib
        elif layer == "huawei_l0_l1":
            if direction == "up":
                l0_to_l1[src_id] += gib
                l1_from_l0[dst_id] += gib
            elif direction == "down":
                l1_to_l0[src_id] += gib
                l0_from_l1[dst_id] += gib
        elif layer == "huawei_l1_ocs":
            l1_to_ocs[src_id] += gib
            l1_from_ocs[dst_id] += gib
            ocs_matrix[src_id, dst_id] += gib

    l1_rows: list[dict[str, object]] = []
    for l1_id in range(l1_count):
        group, plane, eps = l1_parts(l1_id, l1_planes, l1_eps_per_plane)
        l1_rows.append(
            {
                "l1_id": l1_id,
                "label": l1_label(l1_id, l1_planes, l1_eps_per_plane),
                "group": group,
                "plane": plane,
                "eps": eps,
                "src_from_l0_gib": f"{l1_from_l0[l1_id]:.9f}",
                "src_to_ocs_gib": f"{l1_to_ocs[l1_id]:.9f}",
                "dst_from_ocs_gib": f"{l1_from_ocs[l1_id]:.9f}",
                "dst_to_l0_gib": f"{l1_to_l0[l1_id]:.9f}",
            }
        )
    write_csv(
        out_dir / "actual_l1_eps_load.csv",
        l1_rows,
        [
            "l1_id",
            "label",
            "group",
            "plane",
            "eps",
            "src_from_l0_gib",
            "src_to_ocs_gib",
            "dst_from_ocs_gib",
            "dst_to_l0_gib",
        ],
    )

    l0_rows: list[dict[str, object]] = []
    for l0_id in range(max_l0):
        group, tray, plane = l0_parts(l0_id, ranks_per_group, ranks_per_tray, l1_planes)
        l0_rows.append(
            {
                "l0_id": l0_id,
                "group": group,
                "tray": tray,
                "plane": plane,
                "src_host_up_gib": f"{l0_host_up[l0_id]:.9f}",
                "src_to_l1_gib": f"{l0_to_l1[l0_id]:.9f}",
                "dst_from_l1_gib": f"{l0_from_l1[l0_id]:.9f}",
                "dst_host_down_gib": f"{l0_host_down[l0_id]:.9f}",
            }
        )
    write_csv(
        out_dir / "actual_l0_eps_load.csv",
        l0_rows,
        [
            "l0_id",
            "group",
            "tray",
            "plane",
            "src_host_up_gib",
            "src_to_l1_gib",
            "dst_from_l1_gib",
            "dst_host_down_gib",
        ],
    )

    matrix_rows: list[dict[str, object]] = []
    for src in range(l1_count):
        src_group, src_plane, src_eps = l1_parts(src, l1_planes, l1_eps_per_plane)
        for dst in range(l1_count):
            value = ocs_matrix[src, dst]
            if value <= 0:
                continue
            dst_group, dst_plane, dst_eps = l1_parts(dst, l1_planes, l1_eps_per_plane)
            matrix_rows.append(
                {
                    "src_l1_id": src,
                    "src_group": src_group,
                    "src_plane": src_plane,
                    "src_eps": src_eps,
                    "dst_l1_id": dst,
                    "dst_group": dst_group,
                    "dst_plane": dst_plane,
                    "dst_eps": dst_eps,
                    "gib": f"{value:.9f}",
                }
            )
    write_csv(
        out_dir / "actual_ocs_l1_matrix.csv",
        matrix_rows,
        [
            "src_l1_id",
            "src_group",
            "src_plane",
            "src_eps",
            "dst_l1_id",
            "dst_group",
            "dst_plane",
            "dst_eps",
            "gib",
        ],
    )

    summary_rows: list[dict[str, object]] = []
    for scope, values in [
        ("src_l0_send", l0_to_l1),
        ("src_l1_send", l1_from_l0),
        ("dst_l1_recv", l1_to_l0),
        ("dst_l0_recv", l0_from_l1),
        ("l0_host_up", l0_host_up),
        ("l0_host_down", l0_host_down),
        ("l1_from_l0", l1_from_l0),
        ("l1_to_l0", l1_to_l0),
        ("ocs_l1_egress", l1_to_ocs),
        ("ocs_l1_ingress", l1_from_ocs),
        ("ocs_l1_pairs", ocs_matrix[ocs_matrix > 0]),
    ]:
        for metric, value in stats(values).items():
            summary_rows.append({"scope": scope, "metric": metric, "value": f"{value:.9f}"})
    write_csv(out_dir / "actual_load_summary.csv", summary_rows, ["scope", "metric", "value"])

    plot_l0_by_pod(
        l0_to_l1,
        out_dir / "src_l0_send_load_by_pod.png",
        f"{algo_label}: source L0 EPS send load by pod",
        groups,
        ranks_per_group,
        ranks_per_tray,
        l1_planes,
    )
    plot_l1_by_pod(
        l1_from_l0,
        out_dir / "src_l1_send_load_by_pod.png",
        f"{algo_label}: source L1 EPS send load by pod (from L0)",
        groups,
        l1_planes,
        l1_eps_per_plane,
    )
    plot_l1_by_pod(
        l1_to_l0,
        out_dir / "dst_l1_recv_load_by_pod.png",
        f"{algo_label}: destination L1 EPS receive load by pod (to L0)",
        groups,
        l1_planes,
        l1_eps_per_plane,
    )
    plot_l0_by_pod(
        l0_from_l1,
        out_dir / "dst_l0_recv_load_by_pod.png",
        f"{algo_label}: destination L0 EPS receive load by pod",
        groups,
        ranks_per_group,
        ranks_per_tray,
        l1_planes,
    )
    plot_ocs_matrix(
        ocs_matrix,
        out_dir / "ocs_l1_matrix.png",
        f"{algo_label}: actual OCS L1-to-L1 traffic matrix",
        groups,
        l1_per_group,
    )

    with (out_dir / "README.txt").open("w") as f:
        f.write("Huawei actual load analysis\n")
        f.write("===========================\n")
        f.write(f"Algorithm: {algo_label}\n")
        f.write("Source: output_metrics/link_info.csv + link_load_1ms.csv\n\n")
        f.write("EPS send/receive semantics:\n")
        f.write("- src_l1_send is L0->L1 upward traffic into the chosen source L1-EPS.\n")
        f.write("- dst_l1_recv is L1->L0 downward traffic from the chosen destination L1-EPS.\n")
        f.write("- L1->OCS and OCS->L1 hops are OCS-internal traffic and are reported separately as ocs_l1_egress/ocs_l1_ingress and ocs_l1_matrix.\n\n")
        f.write("Key figures:\n")
        f.write("- src_l0_send_load_by_pod.png\n")
        f.write("- src_l1_send_load_by_pod.png\n")
        f.write("- dst_l1_recv_load_by_pod.png\n")
        f.write("- dst_l0_recv_load_by_pod.png\n")
        f.write("- ocs_l1_matrix.png\n")

    print(f"Wrote Huawei actual load analysis to {out_dir}")
    for name in [
        "src_l0_send_load_by_pod.png",
        "src_l1_send_load_by_pod.png",
        "dst_l1_recv_load_by_pod.png",
        "dst_l0_recv_load_by_pod.png",
        "ocs_l1_matrix.png",
    ]:
        print(f"  {out_dir / name}")


if __name__ == "__main__":
    main()
