#!/usr/bin/env python3
"""Plot DeepSeek-R1 EP256 input traffic and HTSIM result summaries."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


MIB = 1024**2
GIB = 1024**3


def parse_args() -> argparse.Namespace:
    tests_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=tests_dir / "deepseek_r1_empirical")
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--plots-dir", type=Path, default=None)
    parser.add_argument("--ranks", type=int, default=256)
    parser.add_argument("--hosts-per-tor", type=int, default=4)
    return parser.parse_args()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_pair_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def read_config(out_dir: Path) -> dict[str, object]:
    path = out_dir / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def read_run_config_value(run_dir: Path, key: str) -> str | None:
    path = run_dir / "run_config.txt"
    if not path.exists():
        return None
    prefix = f"{key}:"
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return None


def huawei_algorithm_label(run_dir: Path) -> str:
    mode = read_run_config_value(run_dir, "huawei_ocs_mode")
    if mode is None:
        name = run_dir.name.lower()
        if "spraypoint" in name:
            mode = "spraypoint"
        elif "ksp" in name:
            mode = "ksp"
    labels = {
        "ksp": "KSP",
        "spraypoint": "SprayPoint",
        "l2_eps": "L2-EPS",
    }
    return labels.get((mode or "").lower(), mode or "unknown")


def traffic_scope_label(config: dict[str, object]) -> str:
    layers = int(config.get("moe_layers", 58))
    include_combine = bool(config.get("include_combine", True))
    layer_text = "1 MoE layer" if layers == 1 else f"{layers} MoE layers"
    traffic_text = "dispatch + combine" if include_combine else "dispatch only"
    return f"{layer_text}, {traffic_text}"


def save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_flow_size_distribution(rows: list[dict[str, str]], plots_dir: Path, scope_label: str) -> None:
    sizes_mib = np.array([int(row["total_bytes_all_layers"]) / MIB for row in rows], dtype=np.float64)
    ordered = np.sort(sizes_mib)
    cdf = np.arange(1, len(ordered) + 1) / len(ordered)

    fig, ax = plt.subplots(figsize=(8.8, 5.6))
    ax.plot(ordered, cdf, color="#1f77b4", linewidth=2.2)
    ax.set_title("DeepSeek-R1 Expert-to-Expert Flow Size CDF")
    ax.set_xlabel(f"Ordered pair flow size ({scope_label}) (MiB)")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.28)
    save(fig, plots_dir / "expert2expert_flow_size_cdf.png")

    fig, ax = plt.subplots(figsize=(8.8, 5.6))
    ax.hist(sizes_mib, bins=72, color="#5aa2d6", edgecolor="white", linewidth=0.35)
    ax.axvline(float(np.mean(sizes_mib)), color="#0b4f8a", linestyle="--", linewidth=2, label=f"mean {np.mean(sizes_mib):.1f} MiB")
    ax.set_title("DeepSeek-R1 Expert-to-Expert Flow Size Histogram")
    ax.set_xlabel(f"Ordered pair flow size ({scope_label}) (MiB)")
    ax.set_ylabel("Flow count")
    ax.grid(True, axis="y", alpha=0.25)
    ax.legend()
    save(fig, plots_dir / "expert2expert_flow_size_hist.png")


def read_pooled_heat_cdf(out_dir: Path) -> tuple[np.ndarray, np.ndarray]:
    path = out_dir / "empirical_pooled_heat_cdf.txt"
    if not path.exists():
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    heats: list[float] = []
    cdfs: list[float] = []
    with path.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) != 3:
                raise ValueError(f"{path}: expected rows shaped as 'rank heat cdf'")
            heats.append(float(parts[1]))
            cdfs.append(float(parts[2]))
    return np.array(heats, dtype=np.float64), np.array(cdfs, dtype=np.float64)


def read_sampled_receiver_hotness(out_dir: Path) -> np.ndarray:
    path = out_dir / "sampled_receiver_hotness_distribution.txt"
    if not path.exists():
        return np.array([], dtype=np.float64)
    values: list[float] = []
    with path.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) != 3:
                raise ValueError(f"{path}: expected rows shaped as 'layer dst_rank hotness'")
            values.append(float(parts[2]))
    return np.array(values, dtype=np.float64)


def plot_empirical_sampling(out_dir: Path, plots_dir: Path) -> None:
    pooled_heat, pooled_cdf = read_pooled_heat_cdf(out_dir)
    if pooled_heat.size == 0:
        return

    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    ax.plot(pooled_heat, pooled_cdf, color="#1f77b4", linewidth=2.0)
    for q, ls in [(50, "--"), (90, ":"), (95, "-."), (99, (0, (3, 1, 1, 1)))]:
        v = float(np.percentile(pooled_heat, q))
        ax.axvline(v, color="#0b4f8a", linestyle=ls, linewidth=1.4, alpha=0.85, label=f"p{q}={v:.0f}")
    ax.set_title("Pooled Empirical Expert Hotness CDF")
    ax.set_xlabel("Expert hotness value from all decode_*.csv")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.25)
    ax.legend(frameon=True)
    save(fig, plots_dir / "empirical_pooled_heat_cdf.png")

    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    ax.hist(pooled_heat, bins=100, color="#5aa6d8", edgecolor="white", linewidth=0.25)
    ax.set_title("Pooled Empirical Expert Hotness Histogram")
    ax.set_xlabel("Expert hotness value from all decode_*.csv")
    ax.set_ylabel("Count")
    ax.grid(True, axis="y", alpha=0.22)
    save(fig, plots_dir / "empirical_pooled_heat_hist.png")

    sampled = read_sampled_receiver_hotness(out_dir)
    if sampled.size == 0:
        return
    sampled_ordered = np.sort(sampled)
    sampled_cdf = np.arange(1, len(sampled_ordered) + 1) / len(sampled_ordered)

    fig, ax = plt.subplots(figsize=(9.2, 5.8))
    ax.plot(pooled_heat, pooled_cdf, color="#9aa8b4", linewidth=1.8, alpha=0.9, label="pooled CDF")
    ax.step(sampled_ordered, sampled_cdf, where="post", color="#1f77b4", linewidth=2.0, label="sampled receiver distribution")
    ax.set_title("Pooled CDF vs Sampled Receiver Hotness")
    ax.set_xlabel("Expert hotness value")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.25)
    ax.legend(frameon=True)
    save(fig, plots_dir / "sampled_receiver_hotness_cdf.png")


def build_expert_matrix(rows: list[dict[str, str]], ranks: int) -> np.ndarray:
    matrix = np.zeros((ranks, ranks), dtype=np.float64)
    for row in rows:
        matrix[int(row["src_rank"]), int(row["dst_rank"])] = int(row["total_bytes_all_layers"])
    return matrix


def build_tor_matrix(expert_matrix: np.ndarray, hosts_per_tor: int) -> np.ndarray:
    ranks = expert_matrix.shape[0]
    tors = math.ceil(ranks / hosts_per_tor)
    tor_matrix = np.zeros((tors, tors), dtype=np.float64)
    for src in range(ranks):
        src_tor = src // hosts_per_tor
        for dst in range(ranks):
            dst_tor = dst // hosts_per_tor
            tor_matrix[src_tor, dst_tor] += expert_matrix[src, dst]
    return tor_matrix


def offdiag_values(matrix: np.ndarray) -> np.ndarray:
    return matrix[~np.eye(matrix.shape[0], dtype=bool)]


def summarize_matrix(name: str, matrix: np.ndarray, unit: float) -> dict[str, float | str]:
    values = offdiag_values(matrix)
    mean = float(np.mean(values))
    std = float(np.std(values))
    return {
        "matrix": name,
        "offdiag_count": float(values.size),
        "min": float(np.min(values) / unit),
        "p50": float(np.percentile(values, 50) / unit),
        "p95": float(np.percentile(values, 95) / unit),
        "p99": float(np.percentile(values, 99) / unit),
        "max": float(np.max(values) / unit),
        "mean": float(mean / unit),
        "std": float(std / unit),
        "cv": float(std / mean if mean else math.nan),
    }


def plot_matrix(matrix: np.ndarray, path: Path, title: str, unit_label: str, unit: float) -> None:
    plot_data = matrix.copy() / unit
    np.fill_diagonal(plot_data, np.nan)
    cmap = plt.get_cmap("Blues").copy()
    cmap.set_bad(color="white")

    fig, ax = plt.subplots(figsize=(8.8, 7.4))
    im = ax.imshow(plot_data, interpolation="nearest", aspect="equal", cmap=cmap, vmin=0)
    ax.set_title(title)
    ax.set_xlabel("Destination")
    ax.set_ylabel("Source")
    cbar = fig.colorbar(im, ax=ax, pad=0.012)
    cbar.set_label(unit_label)
    save(fig, path)


def write_matrix_summary(path: Path, expert_matrix: np.ndarray, tor_matrix: np.ndarray) -> None:
    rows = [
        summarize_matrix("expert2expert_offdiag_mib", expert_matrix, MIB),
        summarize_matrix("tor2tor_offdiag_gib", tor_matrix, GIB),
    ]
    fields = ["matrix", "offdiag_count", "min", "p50", "p95", "p99", "max", "mean", "std", "cv"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def plot_traffic_matrices(
    rows: list[dict[str, str]],
    plots_dir: Path,
    ranks: int,
    hosts_per_tor: int,
    scope_label: str,
) -> None:
    expert_matrix = build_expert_matrix(rows, ranks)
    tor_matrix = build_tor_matrix(expert_matrix, hosts_per_tor)
    plot_matrix(
        expert_matrix,
        plots_dir / "expert2expert_traffic_matrix.png",
        "DeepSeek-R1 Expert-to-Expert Traffic Matrix",
        f"Traffic ({scope_label}) (MiB)",
        MIB,
    )
    plot_matrix(
        tor_matrix,
        plots_dir / "tor2tor_traffic_matrix.png",
        "DeepSeek-R1 ToR-to-ToR Traffic Matrix",
        f"Traffic ({scope_label}) (GiB)",
        GIB,
    )
    write_matrix_summary(plots_dir / "traffic_matrix_summary.csv", expert_matrix, tor_matrix)


def plot_receive_distribution(out_dir: Path, plots_dir: Path) -> None:
    path = out_dir / "expert_receive_by_layer.csv"
    if not path.exists():
        return
    rows = read_pair_rows(path)
    tokens = np.array([int(row["receive_tokens"]) for row in rows], dtype=np.float64)
    ordered = np.sort(tokens)
    cdf = np.arange(1, len(ordered) + 1) / len(ordered)

    fig, ax = plt.subplots(figsize=(8.8, 5.6))
    ax.plot(ordered, cdf, color="#1f77b4", linewidth=2.2)
    ax.set_title("Per-Layer Destination Expert Receive Tokens CDF")
    ax.set_xlabel("Receive token assignments per expert per MoE layer")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.28)
    save(fig, plots_dir / "expert_receive_tokens_cdf.png")


def read_flows_info(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def plot_htsim_results(run_dir: Path) -> bool:
    result_dir = run_dir / "output_metrics"
    ensure_dir(result_dir)
    flows_path = run_dir / "output_metrics" / "flowsInfo.csv"
    rows = read_flows_info(flows_path)
    if not rows:
        print(f"[plot] skip HTSIM result plots; missing or empty {flows_path}")
        return False

    sizes_mib = np.array([int(row["flowSizeBytes"]) / MIB for row in rows], dtype=np.float64)
    fct_ms = np.array([float(row["fctNs"]) / 1e6 for row in rows], dtype=np.float64)
    packets = np.array([int(row.get("totalPackets", "0") or 0) for row in rows], dtype=np.float64)
    ooo = np.array([int(row.get("oooCount", "0") or 0) for row in rows], dtype=np.float64)

    ordered = np.sort(fct_ms)
    cdf = np.arange(1, len(ordered) + 1) / len(ordered)
    fig, ax = plt.subplots(figsize=(8.8, 5.6))
    ax.plot(ordered, cdf, color="#1f77b4", linewidth=2.2)
    ax.set_title("HTSIM UEC Flow Completion Time CDF")
    ax.set_xlabel("FCT (ms)")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.28)
    save(fig, result_dir / "htsim_fct_cdf.png")

    ordered_sizes = np.sort(sizes_mib)
    cdf_sizes = np.arange(1, len(ordered_sizes) + 1) / len(ordered_sizes)
    fig, ax = plt.subplots(figsize=(8.8, 5.6))
    ax.plot(ordered_sizes, cdf_sizes, color="#1f77b4", linewidth=2.2)
    ax.set_title("HTSIM Flow Size CDF")
    ax.set_xlabel("Flow size (MiB)")
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.28)
    save(fig, result_dir / "htsim_flow_size_cdf.png")

    fig, ax = plt.subplots(figsize=(8.8, 5.6))
    ax.scatter(sizes_mib, fct_ms, s=7, alpha=0.28, color="#1f77b4", linewidths=0)
    ax.set_title("HTSIM Flow Size vs FCT")
    ax.set_xlabel("Flow size (MiB)")
    ax.set_ylabel("FCT (ms)")
    ax.grid(True, alpha=0.22)
    save(fig, result_dir / "htsim_flow_size_vs_fct.png")

    def pct(values: np.ndarray, q: float) -> float:
        return float(np.percentile(values, q))

    summary = f"""HTSIM result summary
====================
flows: {len(rows)}

flow_size_mib min/p50/p95/p99/max:
  {pct(sizes_mib, 0):.3f} {pct(sizes_mib, 50):.3f} {pct(sizes_mib, 95):.3f} {pct(sizes_mib, 99):.3f} {pct(sizes_mib, 100):.3f}

fct_ms min/p50/p95/p99/max:
  {pct(fct_ms, 0):.3f} {pct(fct_ms, 50):.3f} {pct(fct_ms, 95):.3f} {pct(fct_ms, 99):.3f} {pct(fct_ms, 100):.3f}

total_packets: {int(np.sum(packets))}
"""
    (result_dir / "htsim_summary.txt").write_text(summary)
    return True


def plot_link_load_results(run_dir: Path) -> bool:
    result_dir = run_dir / "output_metrics"
    info_path = result_dir / "link_info.csv"
    load_path = result_dir / "link_load_1ms.csv"
    if not info_path.exists() or not load_path.exists():
        return False

    link_info: dict[int, dict[str, str]] = {}
    link_totals_by_group: dict[tuple[str, str], int] = {}
    with info_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("link_id"):
                continue
            try:
                link_info[int(row["link_id"])] = row
            except ValueError:
                continue
            key = (row.get("layer", "unknown"), row.get("direction", "unknown"))
            link_totals_by_group[key] = link_totals_by_group.get(key, 0) + 1

    grouped: dict[tuple[str, str], dict[int, list[tuple[float, float]]]] = {}
    totals: dict[int, int] = {}
    samples_by_group: dict[tuple[str, str], list[float]] = {}
    with load_path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                link_id = int(row["link_id"])
                time_ms = float(row["time_ms"])
                throughput = float(row["throughput_gbps"])
                bytes_sent = int(row["bytes"])
            except (TypeError, ValueError):
                continue
            meta = link_info.get(link_id)
            if not meta:
                continue
            key = (meta.get("layer", "unknown"), meta.get("direction", "unknown"))
            grouped.setdefault(key, {}).setdefault(link_id, []).append((time_ms, throughput))
            samples_by_group.setdefault(key, []).append(throughput)
            totals[link_id] = totals.get(link_id, 0) + bytes_sent

    if not grouped:
        return False

    def plot_link_group(ax: plt.Axes, layer: str, direction: str, title: str) -> None:
        per_link = grouped.get((layer, direction), {})
        all_times = sorted({t for points in per_link.values() for t, _ in points})
        for points in per_link.values():
            if not points:
                continue
            points.sort()
            point_map = {t: y for t, y in points}
            xs = all_times
            ys = [point_map.get(t, 0.0) for t in all_times]
            ax.plot(xs, ys, color="#5aa2d6", alpha=0.16, linewidth=0.65)

        total_links = link_totals_by_group.get((layer, direction), len(per_link))
        ax.set_title(f"{title} ({len(per_link)}/{total_links} active links)")
        ax.grid(True, alpha=0.22)
        ax.set_ylim(0, 100)

    observed_layers = {layer for layer, _ in grouped}
    observed_directions = {direction for _, direction in grouped}
    if any(layer.startswith("huawei_") for layer in observed_layers):
        layer_order = ["huawei_l1_ocs", "huawei_l0_l1", "huawei_host_l0"]
        direction_order = ["cross", "up", "down"]

        fig = plt.figure(figsize=(12.5, 9.2))
        grid = fig.add_gridspec(3, 2, hspace=0.38, wspace=0.22)
        axes = [
            fig.add_subplot(grid[0, :]),
            fig.add_subplot(grid[1, 0]),
            fig.add_subplot(grid[1, 1]),
            fig.add_subplot(grid[2, 0]),
            fig.add_subplot(grid[2, 1]),
        ]
        fig.suptitle(f"Per-Link Throughput by Huawei Layer - {huawei_algorithm_label(run_dir)}", fontsize=16)
        panels = [
            (axes[0], "huawei_l1_ocs", "cross", "L1-OCS cross"),
            (axes[1], "huawei_l0_l1", "up", "L0-L1 up"),
            (axes[2], "huawei_l0_l1", "down", "L0-L1 down"),
            (axes[3], "huawei_host_l0", "up", "Host-L0 up"),
            (axes[4], "huawei_host_l0", "down", "Host-L0 down"),
        ]
        for ax, layer, direction, title in panels:
            plot_link_group(ax, layer, direction, title)
            if ax in axes[3:]:
                ax.set_xlabel("Time (ms)")
            if ax in (axes[0], axes[1], axes[3]):
                ax.set_ylabel("Throughput (Gbps)")
    else:
        layer_order = ["agg_core", "tor_agg", "host_tor"]
        direction_order = ["up", "down"]
        layer_order += sorted(observed_layers - set(layer_order))
        direction_order += sorted(observed_directions - set(direction_order))
        fig, axes = plt.subplots(
            len(layer_order),
            len(direction_order),
            figsize=(13.5, 10.5),
            sharex=True,
            sharey=True,
            squeeze=False,
        )
        fig.suptitle("Per-Link Throughput by Topology Layer", fontsize=16)

        for row_i, layer in enumerate(layer_order):
            for col_i, direction in enumerate(direction_order):
                ax = axes[row_i][col_i]
                plot_link_group(ax, layer, direction, f"{layer} {direction}")
                if row_i == len(layer_order) - 1:
                    ax.set_xlabel("Time (ms)")
                if col_i == 0:
                    ax.set_ylabel("Throughput (Gbps)")

    save(fig, result_dir / "link_load_by_layer.png")

    summary_path = result_dir / "link_load_summary.csv"
    fields = [
        "layer",
        "direction",
        "active_links",
        "active_samples",
        "total_bytes",
        "per_link_total_bytes_mean",
        "per_link_total_bytes_cv",
        "throughput_active_p50_gbps",
        "throughput_active_p99_gbps",
        "throughput_active_max_gbps",
    ]
    with summary_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for layer in layer_order:
            for direction in direction_order:
                key = (layer, direction)
                per_link = grouped.get(key, {})
                if not per_link:
                    continue
                link_totals = np.array([totals.get(link_id, 0) for link_id in per_link], dtype=np.float64)
                values = np.array(samples_by_group.get(key, []), dtype=np.float64)
                mean_total = float(np.mean(link_totals)) if link_totals.size else 0.0
                cv_total = float(np.std(link_totals) / mean_total) if mean_total else math.nan
                writer.writerow(
                    {
                        "layer": layer,
                        "direction": direction,
                        "active_links": len(per_link),
                        "active_samples": int(values.size),
                        "total_bytes": int(np.sum(link_totals)),
                        "per_link_total_bytes_mean": mean_total,
                        "per_link_total_bytes_cv": cv_total,
                        "throughput_active_p50_gbps": float(np.percentile(values, 50)) if values.size else math.nan,
                        "throughput_active_p99_gbps": float(np.percentile(values, 99)) if values.size else math.nan,
                        "throughput_active_max_gbps": float(np.max(values)) if values.size else math.nan,
                    }
                )

    return True


def main() -> None:
    args = parse_args()
    run_dir = args.run_dir or args.out_dir / "htsim_run"
    plots_dir = args.plots_dir or args.out_dir / "plots"
    ensure_dir(plots_dir)

    network_pairs = args.out_dir / "expert2expert_empirical_network_pairs.csv"
    all_pairs = args.out_dir / "expert2expert_empirical_all_pairs.csv"
    if not network_pairs.exists() or not all_pairs.exists():
        raise FileNotFoundError(f"missing generated pair CSVs under {args.out_dir}")

    network_rows = read_pair_rows(network_pairs)
    all_rows = read_pair_rows(all_pairs)
    config = read_config(args.out_dir)
    scope_label = traffic_scope_label(config)
    plot_empirical_sampling(args.out_dir, plots_dir)
    plot_flow_size_distribution(network_rows, plots_dir, scope_label)
    plot_traffic_matrices(all_rows, plots_dir, args.ranks, args.hosts_per_tor, scope_label)
    plot_receive_distribution(args.out_dir, plots_dir)
    wrote_results = plot_htsim_results(run_dir)
    wrote_link_load = plot_link_load_results(run_dir)
    print(f"[plot] wrote input PNG plots to {plots_dir}")
    if wrote_results:
        print(f"[plot] wrote HTSIM result plots to {run_dir / 'output_metrics'}")
    if wrote_link_load:
        print(f"[plot] wrote link-load plots to {run_dir / 'output_metrics'}")


if __name__ == "__main__":
    main()
