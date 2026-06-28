#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified Expander Graph Generator (Pure / Ramanujan).

Generates random or Ramanujan-like expander graphs for data-center simulation.
Outputs topology files compatible with HTSIM, including adjacency matrices and
multi-path routing tables (KSP or ECMP modes). Optionally plots and saves the
graph structure with spectral metrics.

Example:
    # Generate a random regular graph
    python expander_gen.py -n 130 -k 7 -c 5 -m pure -p ksp -l 4 --plot

    # Generate a Ramanujan-like expander graph
    python expander_gen.py -n 130 -k 7 -c 5 -m rama -p ecmp --plot
"""

import argparse
import hashlib
import math
import time
import numpy as np
import networkx as nx
import os
import matplotlib.pyplot as plt
from collections import deque
from itertools import islice
from math import sqrt
from scipy.sparse.linalg import eigsh
from scipy.sparse import csr_matrix

def limit_ecmp(G, src, dst, total_limit=8):
    """
    Round-robin next-hop ECMP:
    - Ensures next-hop diversity
    - Ensures strict shortest-path correctness
    - Enumerates paths in breadth-first manner across next-hops
    """

    # 1. Compute global shortest path length
    try:
        shortest_len = nx.shortest_path_length(G, src, dst)
    except nx.NetworkXNoPath:
        return []

    results = []
    seen = set()

    # 2. Create generator for each next-hop
    nh_generators = {}
    for nh in G[src]:
        try:
            nh_generators[nh] = nx.shortest_simple_paths(G, nh, dst)
        except nx.NetworkXNoPath:
            pass

    # 3. Round-robin over all next-hops
    active_nh = list(nh_generators.keys())

    while active_nh and len(results) < total_limit:

        next_round = []

        for nh in active_nh:
            gen = nh_generators[nh]

            # Try to fetch exactly ONE path from this nh in this round
            try:
                tail = next(gen)
            except StopIteration:
                continue  # this nh exhausted; do not include in next round

            full = [src] + tail
            hop = len(full) - 1

            # First path via this nh must be shortest → otherwise remove nh
            if hop != shortest_len:
                continue

            tup = tuple(full)
            if tup not in seen:
                seen.add(tup)
                results.append(full)

            # keep this nh active for next round
            next_round.append(nh)

            # global limit reached
            if len(results) >= total_limit:
                break

        active_nh = next_round

    return results



class SprayPointRouter:
    """Destination-based SprayPoint path enumerator for explicit HTSIM paths.

    SprayPoint forwarding is split into a source-only spray step followed by
    destination-specific pointing rules. This class builds the pointing state
    per destination and expands the resulting next-hop choices into explicit
    paths for the existing HTSIM exporter.
    """

    def __init__(self, G: nx.Graph, waypoint_count: int = 4,
                 next_hops: int = 2, levels: int | None = None):
        self.G = G
        self.nodes = sorted(G.nodes())
        self.n = len(self.nodes)
        self.p = max(1, waypoint_count)
        self.h = max(1, next_hops)
        self.levels_override = levels
        degrees = [G.degree(node) for node in self.nodes]
        self.degree = int(round(sum(degrees) / max(len(degrees), 1)))
        self.levels = self._derive_levels()
        self._state_cache = {}
        print(f"[SprayPoint] p={self.p}, h={self.h}, levels={self.levels}, n={self.n}, degree~={self.degree}")

    def _derive_levels(self) -> int:
        if self.levels_override is not None:
            return max(1, self.levels_override)
        if self.p <= 1 or self.degree <= 0:
            return 1
        ratio = self.n / max(2 * self.degree * self.degree, 1)
        if ratio <= 1:
            return 1
        return max(1, math.ceil(math.log(ratio, self.p)))

    @staticmethod
    def _rank_key(*parts) -> int:
        raw = "|".join(map(str, parts)).encode("utf-8")
        return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")

    def _pick(self, items, count, *salt):
        ordered = sorted(set(items), key=lambda item: self._rank_key(*salt, item))
        return ordered[:max(0, min(count, len(ordered)))]

    def _shortest_next_hops(self, u, dist_map, count, *salt):
        candidates = [v for v in self.G.neighbors(u) if v in dist_map]
        if not candidates:
            return []
        best = min(dist_map[v] for v in candidates)
        return self._pick([v for v in candidates if dist_map[v] == best], count, *salt)

    def _multi_source_distances(self, sources):
        dist = {source: 0 for source in sources}
        queue = deque(sources)
        while queue:
            cur = queue.popleft()
            for neigh in self.G.neighbors(cur):
                if neigh not in dist:
                    dist[neigh] = dist[cur] + 1
                    queue.append(neigh)
        return dist

    def _build_state(self, dst):
        if dst in self._state_cache:
            return self._state_cache[dst]

        wp_levels = []
        wp0 = set(self.G.neighbors(dst))
        wp_levels.append(wp0)

        assigned = {dst} | set(wp0)
        node_level = {node: 0 for node in wp0}

        for level in range(1, self.levels + 1):
            cur = set()
            for prev in sorted(wp_levels[level - 1]):
                candidates = [v for v in self.G.neighbors(prev) if v not in assigned]
                cur.update(self._pick(candidates, self.p, "wp", dst, level, prev))
            wp_levels.append(cur)
            for node in cur:
                node_level[node] = level
            assigned.update(cur)

        wp_last = wp_levels[-1]
        inner_ring = set()
        for node in wp_last:
            for neigh in self.G.neighbors(node):
                if neigh not in assigned:
                    inner_ring.add(neigh)

        assigned_with_ir = assigned | inner_ring
        outer_ring = set(self.nodes) - assigned_with_ir

        dist_to_dst = nx.single_source_shortest_path_length(self.G, dst)
        dist_to_ir = {}
        if inner_ring:
            dist_to_ir = self._multi_source_distances(inner_ring)

        parents = {}
        for node in self.nodes:
            if node == dst:
                continue

            if node in node_level:
                level = node_level[node]
                target = {dst} if level == 0 else wp_levels[level - 1]
                candidates = [v for v in self.G.neighbors(node) if v in target]
                parents[node] = self._pick(candidates, self.h, "parent", dst, node, level)
            elif node in inner_ring:
                candidates = [v for v in self.G.neighbors(node) if v in wp_last]
                parents[node] = self._pick(candidates, self.h, "parent-ir", dst, node)
            else:
                if dist_to_ir:
                    parents[node] = self._shortest_next_hops(node, dist_to_ir, self.h, "parent-or", dst, node)
                else:
                    parents[node] = []

            if not parents[node]:
                parents[node] = self._shortest_next_hops(node, dist_to_dst, self.h, "fallback", dst, node)

        state = {
            "wp_levels": wp_levels,
            "inner_ring": inner_ring,
            "outer_ring": outer_ring,
            "parents": parents,
        }
        self._state_cache[dst] = state
        return state

    def paths(self, src, dst, limit):
        if src == dst:
            return []

        state = self._build_state(dst)
        max_nodes = self.levels + 8
        first_hops = self._pick(list(self.G.neighbors(src)), self.G.degree(src), "spray", src, dst)

        # SprayPoint's first hop is the high-fanout spray step. Build each
        # spray branch separately, then interleave them so a finite HTSIM path
        # limit still preserves first-hop diversity before adding duplicates
        # from the same spray next-hop.
        per_first_hop = []
        seen = set()
        for first in first_hops:
            branch = []
            self._extend([src, first], src, dst, state, branch, seen, limit, max_nodes)
            if branch:
                per_first_hop.append(branch)

        results = []
        offset = 0
        while len(results) < limit:
            progressed = False
            for branch in per_first_hop:
                if offset < len(branch):
                    results.append(branch[offset])
                    progressed = True
                    if len(results) >= limit:
                        break
            if not progressed:
                break
            offset += 1

        return results

    def _extend(self, path, src, dst, state, results, seen, limit, max_nodes):
        if len(results) >= limit:
            return

        cur = path[-1]
        if cur == dst:
            key = tuple(path)
            if key not in seen:
                seen.add(key)
                results.append(path)
            return

        if len(path) >= max_nodes:
            return

        for nxt in state["parents"].get(cur, []):
            if nxt in path:
                if nxt == src and path.count(src) == 1:
                    pass
                else:
                    continue
            self._extend(path + [nxt], src, dst, state, results, seen, limit, max_nodes)
            if len(results) >= limit:
                break


def load_unique_pairs(flow_files, hosts_per_tor):
    pairs = set()

    total_lines = 0
    valid = 0
    invalid = 0
    invalid_same_tor = 0
    invalid_other = 0

    for flow_file in flow_files:
        with open(flow_file, "r") as f:
            for line in f:
                total_lines += 1
                line = line.strip()

                # Skip blank lines
                if not line:
                    invalid += 1
                    invalid_other += 1
                    continue

                # Skip comments (# ...)
                if line.startswith("#"):
                    invalid += 1
                    invalid_other += 1
                    continue

                s = line.split()
                if len(s) < 2:
                    invalid += 1
                    invalid_other += 1
                    continue

                try:
                    hsrc = int(s[0])
                    hdst = int(s[1])
                except ValueError:
                    invalid += 1
                    invalid_other += 1
                    continue

                # Host → ToR 
                src = hsrc // hosts_per_tor
                dst = hdst // hosts_per_tor

                if src == dst:
                    # same ToR → automatically ignored
                    invalid_same_tor += 1
                    invalid += 1
                    continue

                # valid pair
                valid += 1
                pairs.add((src, dst))
                pairs.add((dst, src))

    print("=== Flow File Statistics ===")
    print(f"Total lines        : {total_lines}")
    print(f"Valid pairs        : {valid}")
    print(f"Invalid lines      : {invalid}")
    print(f"  ├─ Same ToR      : {invalid_same_tor}")
    print(f"  └─ Other invalid : {invalid_other}")
    print(f"Unique src-dst     : {len(pairs)} (after adding reverse pairs)")
    print("================================")

    return pairs



def write_topology_htsim(G: nx.Graph, H: int, dl: int, ul: int,
                         filename: str, K: int, mode: str, flow_file: str,
                         spray_p: int = 4, spray_h: int = 2,
                         spray_levels: int | None = None) -> None:
    """Writes a NetworkX graph as an HTSIM-compatible topology file.

    Added:
        - Streaming write (buffer + flush)
        - Timestamp + delta time
    """

    G = nx.convert_node_labels_to_integers(G, first_label=0)
    N = G.number_of_nodes()
    print(f"Writing HTSIM topology: N={N}, dl={dl}, ul={ul}, mode={mode}, file={filename}")

    # ----------- path functions (unchanged) -----------
    def k_shortest_paths(G, src, dst, K):
        return list(islice(nx.shortest_simple_paths(G, src, dst, weight=None), K))

    def ecmp_shortest_paths(G, src, dst):
        shortest_len = nx.shortest_path_length(G, src, dst)
        paths = []
        for path in nx.all_simple_paths(G, src, dst, cutoff=shortest_len):
            if len(path) - 1 == shortest_len:
                paths.append(path)
        return paths

    spraypoint_router = None
    if mode == "spraypoint":
        spraypoint_router = SprayPointRouter(
            G,
            waypoint_count=spray_p,
            next_hops=spray_h,
            levels=spray_levels,
        )

    # =====================================================
    #                streaming write (NEW)
    # =====================================================
    PRINT_INTERVAL = 100_000
    buffer = []
    total_lines = 0
    total_paths = 0
    t_last = time.time()

    # clear old file
    open(filename, "w").close()

    def flush(force=False):
        nonlocal buffer, total_lines, t_last
        if force or len(buffer) >= PRINT_INTERVAL:
            with open(filename, "a", encoding="utf-8") as f:
                for ln in buffer:
                    f.write(ln + "\n")
            now = time.time()
            delta = now - t_last
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"written {total_lines:,} lines, delta={delta:.2f}s")
            t_last = now
            buffer = []

    # =====================================================
    #                    write content
    # =====================================================

    # Header
    buffer.append(f"{H} {dl} {ul} {N}")
    total_lines += 1
    flush()

    # Adjacency matrix
    A = nx.to_numpy_array(G, dtype=int)
    for i in range(N):
        buffer.append(" ".join(map(str, A[i].astype(int))))
        total_lines += 1
        flush()

    # Multi-path routing table
    pairs = load_unique_pairs(flow_file, dl)
    print(f"[INFO] Writing routing for {len(pairs)} src-dst pairs")

    for src, dst in pairs:
        if src == dst:
            continue

        if mode == "ecmp":
            paths = ecmp_shortest_paths(G, src, dst)
        elif mode == "ksp":
            paths = k_shortest_paths(G, src, dst, K)
        elif mode == "limit_ecmp":
            paths = limit_ecmp(G, src, dst, K)
        elif mode == "spraypoint":
            paths = spraypoint_router.paths(src, dst, K)
        else:
            raise ValueError(f"Unsupported mode: {mode}")

        for path in paths:
            middle = path[1:-1]
            if middle:
                line = f"{src} {dst} " + " ".join(map(str, middle))
            else:
                line = f"{src} {dst}"

            buffer.append(line)
            total_lines += 1
            total_paths += 1
            flush()

    # Final flush
    if buffer:
        with open(filename, "a") as f:
            # write all but the last line with '\n'
            for ln in buffer[:-1]:
                f.write(ln + "\n")
            # write last line WITHOUT '\n'
            f.write(buffer[-1])
        buffer.clear()

    print(f"HTSIM topology written to {filename} (mode={mode}, total {total_paths} paths)")


def build_random_regular_graph(n: int, degree: int, seed: int = None) -> nx.Graph:
    """Builds a random d-regular graph.

    Args:
        n (int): Number of nodes (switches).
        degree (int): Target node degree.
        seed (int, optional): Random seed.

    Returns:
        nx.Graph: A random d-regular graph.

    Raises:
        ValueError: If the degree is invalid or graph cannot exist.
    """
    if degree < 0 or degree > n - 1:
        raise ValueError("degree must be within [0, n-1]")
    if (n * degree) % 2 != 0:
        raise ValueError("n * degree must be even for a regular graph to exist")
    return nx.random_regular_graph(d=degree, n=n, seed=seed)


def const_rama_py(N: int, u: int, seed: int = None, max_trials: int = 2000) -> nx.Graph:
    """Constructs a u-regular Ramanujan-like expander graph.

    The function attempts randomized construction and verifies the
    Ramanujan bound λ₂ ≤ 2√(u-1). If no valid graph is found within
    `max_trials`, it raises an exception.

    Args:
        N (int): Number of switches.
        u (int): Desired degree (uplinks).
        seed (int, optional): Random seed.
        max_trials (int): Maximum number of trials.

    Returns:
        nx.Graph: A Ramanujan-like expander graph.

    Raises:
        RuntimeError: If no valid Ramanujan-like graph is found.
    """
    if not (0 < u < N):
        raise ValueError("Require 0 < u < N.")
    if (u * N) % 2 != 0:
        raise ValueError("u * N must be even to admit a u-regular graph.")

    rng = np.random.default_rng(seed)
    rama_bound = 2 * sqrt(u - 1)

    for trial in range(1, max_trials + 1):
        i_pool, j_pool = np.triu_indices(N, k=1)
        in_pool = np.ones_like(i_pool, dtype=bool)
        A = np.zeros((N, N), dtype=int)
        deg = np.zeros(N, dtype=int)
        npop = (u * N) // 2
        zeroed = np.zeros(N, dtype=bool)

        success = True
        for _ in range(npop):
            available = np.flatnonzero(in_pool)
            if available.size == 0:
                success = False
                break

            pick = rng.choice(available)
            ipick, jpick = i_pool[pick], j_pool[pick]
            in_pool[pick] = False

            # Add edge if both endpoints still have capacity
            if deg[ipick] < u and deg[jpick] < u and A[ipick, jpick] == 0:
                A[ipick, jpick] = 1
                A[jpick, ipick] = 1
                deg[ipick] += 1
                deg[jpick] += 1

                # Remove all candidate edges touching full-degree nodes
                for node in (ipick, jpick):
                    if deg[node] == u and not zeroed[node]:
                        mask = (i_pool == node) | (j_pool == node)
                        in_pool[mask] = False
                        zeroed[node] = True

        if not success or np.min(deg) != u:
            continue

        evals = np.linalg.eigvalsh(A)
        lambda2 = np.sort(np.abs(evals))[-2]
        if lambda2 <= rama_bound:
            print(f"Found Ramanujan-like graph at trial {trial}: λ₂={lambda2:.3f}, bound={rama_bound:.3f}")
            return nx.from_numpy_array(A)

    raise RuntimeError(f"Failed to find Ramanujan-like graph within {max_trials} trials.")


def validate_degrees(G: nx.Graph, target_degree: int) -> bool:
    """Checks if all nodes in the graph have the specified degree."""
    degrees = [deg for _, deg in G.degree()]
    print(f"[validate_degrees] min={min(degrees)}, max={max(degrees)}, target={target_degree}")
    return all(deg == target_degree for deg in degrees)


def pairwise_shortest_path_stats(G: nx.Graph) -> tuple[float, int]:
    """Computes shortest-path statistics for the graph."""
    mean_distance = nx.average_shortest_path_length(G)
    diameter = nx.diameter(G)
    return mean_distance, diameter


def main() -> None:
    """Command-line entry point for the expander generator."""
    parser = argparse.ArgumentParser(description="Unified Expander Graph Generator (pure / rama)")
    parser.add_argument("-n", type=int, default=130, help="number of switches")
    parser.add_argument("-k", type=int, default=8, help="uplink degree (inter-switch)")
    parser.add_argument("-c", type=int, default=0, help="computing degree per node (downlinks)")
    parser.add_argument("-m", type=str, choices=["pure", "rama"], default="pure",
                        help="graph generation mode")
    parser.add_argument("-p", type=str, choices=["ksp", "ecmp", "limit_ecmp", "spraypoint"], default="ksp",
                        help="path generation mode")
    parser.add_argument("--spray-p", type=int, default=4,
                        help="SprayPoint waypoint fanout p")
    parser.add_argument("--spray-h", type=int, default=2,
                        help="SprayPoint pointing next-hop count h")
    parser.add_argument("--spray-levels", type=int, default=None,
                        help="Override SprayPoint waypoint level count; default follows paper formula")
    parser.add_argument("-l", type=int, default=8, help="number of paths for ksp mode")
    parser.add_argument(
        "--flow_file", 
        type=str, 
        nargs="+", 
        required=True,
        help="One or multiple flow files (.htsim) used to extract unique src-dst pairs"
    )
    parser.add_argument("--load", type=str, required=True, help="Traffic load intensity (saved to output filename)")   
    parser.add_argument("--seed", type=int, default=None, help="random seed")
    parser.add_argument("--plot", action="store_true",
                        help="if set, draw and save the generated topology figure")
    parser.add_argument("--traffic", type=str, required=True, help="Traffic type.")
    parser.add_argument("--topo_dir", type=str, default=".",
                        help="output directory for topology file")
    parser.add_argument(
        "--filename",
        type=str,
        default=None,
        help="Optional custom output filename. If not provided, use auto-generated name."
    )
    args = parser.parse_args()

    total_d = args.k + args.c
    print(f"=== Constructing Expander Graph: mode={args.m}, N={args.n}, k={args.k}, c={args.c} ===")

    # === Construct topology ===
    if args.m == "pure":
        G = build_random_regular_graph(n=args.n, degree=args.k, seed=args.seed)
    else:
        G = const_rama_py(N=args.n, u=args.k, seed=args.seed)

    # === Basic topology stats ===
    mean_distance, diameter = pairwise_shortest_path_stats(G)
    H = args.c * args.n
    print(f"switches={args.n}, total_d={total_d}, switch_degree={args.k}, "
          f"computing_degree={args.c}, computing_nodes={H}")
    print(f"edges={G.number_of_edges()}, degree_ok={validate_degrees(G, args.k)}")
    print(f"mean_shortest_path={mean_distance:.4f}, diameter={diameter}")

    # === Spectral properties (both pure and rama) ===
    # A = nx.to_numpy_array(G)
    # eigvals = np.sort(np.abs(np.linalg.eigvals(A)))
    # lambda2 = eigvals[-2]
    # rama_bound = 2 * np.sqrt(args.k - 1)
    # print(f"λ₂={lambda2:.3f}, bound={rama_bound:.3f}, valid={lambda2 <= rama_bound}")

    # === Spectral properties (both pure and rama) ===
    A = nx.to_numpy_array(G)
    A_sparse = csr_matrix(A)
    eigvals, _ = eigsh(A_sparse, k=2, which="LM")
    lambda2 = sorted(np.abs(eigvals))[-2]
    rama_bound = 2 * np.sqrt(args.k - 1)
    print(f"λ₂={lambda2:.3f}, bound={rama_bound:.3f}, valid={lambda2 <= rama_bound}")

    # === Save topology file ===
    os.makedirs(args.topo_dir, exist_ok=True)

    auto_filename = f"{args.traffic}_expander_D{total_d}_C{args.c}_B{args.k}_N{args.n}_{args.m}_{args.p}_load{args.load}.txt"
    final_name = args.filename if args.filename is not None else auto_filename
    filename = os.path.join(args.topo_dir, final_name)
    
    write_topology_htsim(
        G,
        H=H,
        dl=args.c,
        ul=args.k,
        filename=filename,
        K=args.l,
        mode=args.p,
        flow_file=args.flow_file,
        spray_p=args.spray_p,
        spray_h=args.spray_h,
        spray_levels=args.spray_levels
    )

    # === Optional plot ===
    if args.plot:
        fig_name = filename.replace(".txt", ".png")
        pos = nx.spring_layout(G, seed=args.seed)
        plt.figure(figsize=(8, 6))
        nx.draw_networkx_nodes(G, pos, node_size=50, node_color="skyblue", edgecolors="k", linewidths=0.4)
        nx.draw_networkx_edges(G, pos, width=0.5, alpha=0.4)
        title = (f"{args.m.capitalize()} Expander (n={args.n}, k={args.k})\n"
                 f"λ₂={lambda2:.3f}, bound={rama_bound:.3f}")
        plt.title(title)
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(fig_name, dpi=300)
        plt.close()
        print(f"Figure saved to {fig_name}")


if __name__ == "__main__":
    main()
