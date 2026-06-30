#!/usr/bin/env python3
"""Build per-flow Huawei source-route plans for EP all-to-all experiments."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def stable_hash(*values: int) -> int:
    h = 0x9E3779B97F4A7C15
    for value in values:
        x = (int(value) + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
        x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & 0xFFFFFFFFFFFFFFFF
        x = (x ^ (x >> 27)) * 0x94D049BB133111EB & 0xFFFFFFFFFFFFFFFF
        x ^= x >> 31
        h ^= x
        h = ((h << 13) | (h >> 51)) & 0xFFFFFFFFFFFFFFFF
    return h


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def group_of(rank: int, ranks_per_group: int) -> int:
    return rank // ranks_per_group


def tray_of(rank: int, ranks_per_tray: int) -> int:
    return rank // ranks_per_tray


def l0_id(rank: int, plane: int, ranks_per_tray: int, l1_planes: int) -> int:
    return tray_of(rank, ranks_per_tray) * l1_planes + plane


def l1_id(group: int, plane: int, eps: int, l1_planes: int, l1_eps_per_plane: int) -> int:
    return group * l1_planes * l1_eps_per_plane + plane * l1_eps_per_plane + eps


def l1_plane_from_id(l1: int, l1_planes: int, l1_eps_per_plane: int) -> int:
    return (l1 // l1_eps_per_plane) % l1_planes


def empty_assignment(flowid: int, row: dict[str, str], algorithm: str) -> dict[str, int | str]:
    src = int(row["src_physical_rank"])
    dst = int(row["dst_physical_rank"])
    return {
        "flowid": flowid,
        "src": src,
        "dst": dst,
        "src_l0_plane": -1,
        "src_l1_id": -1,
        "dst_l1_id": -1,
        "dst_l0_plane": -1,
        "src_l1_eps": -1,
        "dst_l1_plane": -1,
        "dst_l1_eps": -1,
        "bytes": int(row["total_bytes_all_layers"]),
        "algorithm": algorithm,
    }


def assign_ecmp(
    rows: list[dict[str, str]],
    *,
    l1_planes: int,
    seed: int,
) -> list[dict[str, int | str]]:
    assignments = []
    for flowid, row in enumerate(rows, start=1):
        out = empty_assignment(flowid, row, "ecmp")
        src = int(out["src"])
        dst = int(out["dst"])
        out["src_l0_plane"] = stable_hash(seed, flowid, src, dst) % l1_planes
        assignments.append(out)
    return assignments


def choose_min(loads: dict[int, int], candidates: list[int]) -> int:
    return min(candidates, key=lambda key: (loads.get(key, 0), key))


def assign_lpt(
    rows: list[dict[str, str]],
    *,
    ranks_per_group: int,
    ranks_per_tray: int,
    l1_planes: int,
    l1_eps_per_plane: int,
) -> list[dict[str, int | str]]:
    assignments = [empty_assignment(flowid, row, "lpt") for flowid, row in enumerate(rows, start=1)]

    src_l0_load: dict[int, int] = {}
    src_l1_load: dict[int, int] = {}
    dst_l0_load: dict[int, int] = {}
    dst_l1_load: dict[int, int] = {}

    order = sorted(
        range(len(assignments)),
        key=lambda idx: (-int(assignments[idx]["bytes"]), int(assignments[idx]["flowid"])),
    )

    for idx in order:
        out = assignments[idx]
        size = int(out["bytes"])
        src = int(out["src"])
        dst = int(out["dst"])
        src_group = group_of(src, ranks_per_group)
        dst_group = group_of(dst, ranks_per_group)

        src_l1_candidates = [
            l1_id(src_group, plane, eps, l1_planes, l1_eps_per_plane)
            for plane in range(l1_planes)
            for eps in range(l1_eps_per_plane)
        ]
        chosen_src_l1 = min(
            src_l1_candidates,
            key=lambda l1: (
                src_l1_load.get(l1, 0),
                src_l0_load.get(l0_id(src, l1_plane_from_id(l1, l1_planes, l1_eps_per_plane),
                                      ranks_per_tray, l1_planes), 0),
                l1,
            ),
        )
        src_plane = l1_plane_from_id(chosen_src_l1, l1_planes, l1_eps_per_plane)
        chosen_src_l0 = l0_id(src, src_plane, ranks_per_tray, l1_planes)

        dst_l1_candidates = [
            l1_id(dst_group, plane, eps, l1_planes, l1_eps_per_plane)
            for plane in range(l1_planes)
            for eps in range(l1_eps_per_plane)
        ]
        chosen_dst_l1 = min(
            dst_l1_candidates,
            key=lambda l1: (
                dst_l1_load.get(l1, 0),
                dst_l0_load.get(l0_id(dst, l1_plane_from_id(l1, l1_planes, l1_eps_per_plane),
                                      ranks_per_tray, l1_planes), 0),
                l1,
            ),
        )
        dst_plane = l1_plane_from_id(chosen_dst_l1, l1_planes, l1_eps_per_plane)
        chosen_dst_l0 = l0_id(dst, dst_plane, ranks_per_tray, l1_planes)

        src_l0_load[chosen_src_l0] = src_l0_load.get(chosen_src_l0, 0) + size
        src_l1_load[chosen_src_l1] = src_l1_load.get(chosen_src_l1, 0) + size
        dst_l0_load[chosen_dst_l0] = dst_l0_load.get(chosen_dst_l0, 0) + size
        dst_l1_load[chosen_dst_l1] = dst_l1_load.get(chosen_dst_l1, 0) + size

        out["src_l0_plane"] = src_plane
        out["src_l1_id"] = chosen_src_l1
        out["dst_l1_id"] = chosen_dst_l1
        out["dst_l0_plane"] = dst_plane
        out["src_l1_eps"] = chosen_src_l1 % l1_eps_per_plane
        out["dst_l1_plane"] = (chosen_dst_l1 // l1_eps_per_plane) % l1_planes
        out["dst_l1_eps"] = chosen_dst_l1 % l1_eps_per_plane

    return assignments


def write_csv(path: Path, rows: list[dict[str, int | str]]) -> None:
    fields = [
        "flowid",
        "src",
        "dst",
        "src_l0_plane",
        "src_l1_id",
        "dst_l1_id",
        "dst_l0_plane",
        "src_l1_eps",
        "dst_l1_plane",
        "dst_l1_eps",
        "bytes",
        "algorithm",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, rows: list[dict[str, int | str]]) -> None:
    total = sum(int(row["bytes"]) for row in rows)
    planned_src_l1 = sum(1 for row in rows if int(row["src_l1_id"]) >= 0)
    planned_dst_l1 = sum(1 for row in rows if int(row["dst_l1_id"]) >= 0)
    src_planes = sorted({int(row["src_l0_plane"]) for row in rows if int(row["src_l0_plane"]) >= 0})
    with path.open("w") as f:
        f.write(f"flows,{len(rows)}\n")
        f.write(f"total_bytes,{total}\n")
        f.write(f"planned_src_l1_flows,{planned_src_l1}\n")
        f.write(f"planned_dst_l1_flows,{planned_dst_l1}\n")
        f.write(f"used_src_l0_planes,{':'.join(map(str, src_planes))}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--algorithm", choices=["ecmp", "lpt"], required=True)
    parser.add_argument("--ranks-per-group", type=int, required=True)
    parser.add_argument("--ranks-per-tray", type=int, required=True)
    parser.add_argument("--l1-planes", type=int, required=True)
    parser.add_argument("--l1-eps-per-l1-plane", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rows = read_rows(args.data_dir / "expert2expert_empirical_network_pairs.csv")
    if args.algorithm == "ecmp":
        assignments = assign_ecmp(rows, l1_planes=args.l1_planes, seed=args.seed)
    else:
        assignments = assign_lpt(
            rows,
            ranks_per_group=args.ranks_per_group,
            ranks_per_tray=args.ranks_per_tray,
            l1_planes=args.l1_planes,
            l1_eps_per_plane=args.l1_eps_per_l1_plane,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.out, assignments)
    write_summary(args.out.with_suffix(".summary.csv"), assignments)

    print(f"Huawei route plan: {args.algorithm}")
    print(f"  input flows: {len(rows)}")
    print(f"  output: {args.out}")
    print(f"  summary: {args.out.with_suffix('.summary.csv')}")


if __name__ == "__main__":
    main()
