#!/usr/bin/env python3
"""Build one pooled empirical hotness distribution from decode_*.csv files."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=here)
    parser.add_argument(
        "--out",
        type=Path,
        default=here / "empirical_pooled_distribution.csv",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paths = sorted(
        args.data_dir.glob("decode_*.csv"),
        key=lambda p: int(p.stem.split("_", 1)[1]),
    )
    if not paths:
        raise FileNotFoundError(f"no decode_*.csv found in {args.data_dir}")

    counter: Counter[int] = Counter()
    rows_per_file: int | None = None
    cols_per_file: int | None = None
    for path in paths:
        with path.open(newline="") as f:
            rows = [[int(x) for x in row if x != ""] for row in csv.reader(f) if row]
        if rows_per_file is None:
            rows_per_file = len(rows)
            cols_per_file = len(rows[0]) if rows else 0
        if len(rows) != rows_per_file:
            raise ValueError(f"{path} has {len(rows)} rows, expected {rows_per_file}")
        if any(len(row) != cols_per_file for row in rows):
            raise ValueError(f"{path} has inconsistent column count")
        for row in rows:
            counter.update(row)

    total = sum(counter.values())
    cumulative = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["heat", "count", "pmf", "cdf"])
        writer.writeheader()
        for heat in sorted(counter):
            count = counter[heat]
            cumulative += count
            writer.writerow(
                {
                    "heat": heat,
                    "count": count,
                    "pmf": f"{count / total:.12g}",
                    "cdf": f"{cumulative / total:.12g}",
                }
            )

    print(f"wrote {args.out}")
    print(f"input_files {len(paths)}")
    print(f"matrix_shape {rows_per_file}x{cols_per_file}")
    print(f"samples {total}")
    print(f"unique_heat_values {len(counter)}")


if __name__ == "__main__":
    main()

