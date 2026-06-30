#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstddef>
#include <thread>
#include <unordered_map>
#include <vector>

extern "C" int lpt_assign_core(
    const int32_t* src_ranks,
    const int32_t* dst_ranks,
    const uint64_t* flow_bytes,
    size_t flow_count,
    int nodes,
    int ranks_per_group,
    int ranks_per_tray,
    int l1_planes,
    int l1_eps_per_l1_plane,
    uint64_t* src_l0_load_out,
    uint64_t* dst_l0_load_out,
    size_t l0_count,
    uint64_t* src_l1_load_out,
    uint64_t* dst_l1_load_out,
    size_t l1_count,
    int32_t* src_l0_id_out,
    int32_t* dst_l0_id_out,
    int32_t* src_l1_id_out,
    int32_t* dst_l1_id_out,
    int32_t* src_l0_plane_out,
    int32_t* dst_l0_plane_out,
    int32_t* src_l1_eps_out,
    int32_t* dst_l1_plane_out,
    int32_t* dst_l1_eps_out,
    double* sort_ms_out,
    double* greedy_ms_out) {
    if (!src_ranks || !dst_ranks || !flow_bytes ||
        !src_l0_load_out || !dst_l0_load_out || !src_l1_load_out || !dst_l1_load_out ||
        !src_l0_id_out || !dst_l0_id_out || !src_l1_id_out || !dst_l1_id_out ||
        !src_l0_plane_out || !dst_l0_plane_out ||
        !src_l1_eps_out || !dst_l1_plane_out || !dst_l1_eps_out ||
        !sort_ms_out || !greedy_ms_out) {
        return 1;
    }
    if (nodes <= 0 || ranks_per_group <= 0 || ranks_per_tray <= 0 ||
        l1_planes <= 0 || l1_eps_per_l1_plane <= 0) {
        return 2;
    }
    if (ranks_per_group % ranks_per_tray != 0) {
        return 3;
    }

    const int trays_per_group = ranks_per_group / ranks_per_tray;
    const size_t expected_l0_count = static_cast<size_t>(nodes / ranks_per_tray) *
                                     static_cast<size_t>(l1_planes);
    const size_t expected_l1_count = static_cast<size_t>(nodes / ranks_per_group) *
                                     static_cast<size_t>(l1_planes) *
                                     static_cast<size_t>(l1_eps_per_l1_plane);
    if (l0_count < expected_l0_count || l1_count < expected_l1_count) {
        return 4;
    }

    for (size_t i = 0; i < expected_l0_count; ++i) {
        src_l0_load_out[i] = 0;
        dst_l0_load_out[i] = 0;
    }
    for (size_t i = 0; i < expected_l1_count; ++i) {
        src_l1_load_out[i] = 0;
        dst_l1_load_out[i] = 0;
    }

    using Clock = std::chrono::steady_clock;
    const auto sort_start = Clock::now();

    std::unordered_map<uint64_t, std::vector<size_t>> buckets;
    buckets.reserve(256);
    for (size_t i = 0; i < flow_count; ++i) {
        buckets[flow_bytes[i]].push_back(i);
    }

    std::vector<uint64_t> ordered_sizes;
    ordered_sizes.reserve(buckets.size());
    for (const auto& kv : buckets) {
        ordered_sizes.push_back(kv.first);
    }
    std::sort(ordered_sizes.begin(), ordered_sizes.end(), std::greater<uint64_t>());

    const auto sort_end = Clock::now();
    const auto greedy_start = Clock::now();

    for (uint64_t size : ordered_sizes) {
        const auto& ids = buckets[size];
        for (size_t flow_idx : ids) {
            const int32_t src_rank = src_ranks[flow_idx];
            const int32_t dst_rank = dst_ranks[flow_idx];
            if (src_rank < 0 || src_rank >= nodes) {
                return 5;
            }
            if (dst_rank < 0 || dst_rank >= nodes) {
                return 6;
            }

            const int src_tray = src_rank / ranks_per_tray;
            const int src_group = src_tray / trays_per_group;
            const int dst_tray = dst_rank / ranks_per_tray;
            const int dst_group = dst_rank / ranks_per_group;

            const int src_l0_base = src_tray * l1_planes;
            int src_l0_id = src_l0_base;
            uint64_t src_l0_best_load = src_l0_load_out[src_l0_id];
            for (int candidate = src_l0_base + 1; candidate < src_l0_base + l1_planes; ++candidate) {
                const uint64_t candidate_load = src_l0_load_out[candidate];
                if (candidate_load < src_l0_best_load) {
                    src_l0_id = candidate;
                    src_l0_best_load = candidate_load;
                }
            }
            const int src_plane = src_l0_id - src_l0_base;

            const int src_l1_base = (src_group * l1_planes + src_plane) * l1_eps_per_l1_plane;
            int src_l1_id = src_l1_base;
            uint64_t src_l1_best_load = src_l1_load_out[src_l1_id];
            for (int candidate = src_l1_base + 1; candidate < src_l1_base + l1_eps_per_l1_plane; ++candidate) {
                const uint64_t candidate_load = src_l1_load_out[candidate];
                if (candidate_load < src_l1_best_load) {
                    src_l1_id = candidate;
                    src_l1_best_load = candidate_load;
                }
            }
            const int src_l1_eps = src_l1_id - src_l1_base;

            const int dst_l0_base = dst_tray * l1_planes;
            int dst_l0_id = dst_l0_base;
            uint64_t dst_l0_best_load = dst_l0_load_out[dst_l0_id];
            for (int candidate = dst_l0_base + 1; candidate < dst_l0_base + l1_planes; ++candidate) {
                const uint64_t candidate_load = dst_l0_load_out[candidate];
                if (candidate_load < dst_l0_best_load) {
                    dst_l0_id = candidate;
                    dst_l0_best_load = candidate_load;
                }
            }
            const int dst_l0_plane = dst_l0_id - dst_l0_base;

            const int dst_l1_base = dst_group * l1_planes * l1_eps_per_l1_plane;
            const int dst_l1_limit = dst_l1_base + l1_planes * l1_eps_per_l1_plane;
            int dst_l1_id = dst_l1_base;
            uint64_t dst_l1_best_load = dst_l1_load_out[dst_l1_id];
            for (int candidate = dst_l1_base + 1; candidate < dst_l1_limit; ++candidate) {
                const uint64_t candidate_load = dst_l1_load_out[candidate];
                if (candidate_load < dst_l1_best_load) {
                    dst_l1_id = candidate;
                    dst_l1_best_load = candidate_load;
                }
            }
            const int dst_l1_local = dst_l1_id - dst_l1_base;
            const int dst_l1_plane = dst_l1_local / l1_eps_per_l1_plane;
            const int dst_l1_eps = dst_l1_local % l1_eps_per_l1_plane;

            src_l0_load_out[src_l0_id] += flow_bytes[flow_idx];
            dst_l0_load_out[dst_l0_id] += flow_bytes[flow_idx];
            src_l1_load_out[src_l1_id] += flow_bytes[flow_idx];
            dst_l1_load_out[dst_l1_id] += flow_bytes[flow_idx];

            src_l0_id_out[flow_idx] = src_l0_id;
            dst_l0_id_out[flow_idx] = dst_l0_id;
            src_l1_id_out[flow_idx] = src_l1_id;
            dst_l1_id_out[flow_idx] = dst_l1_id;
            src_l0_plane_out[flow_idx] = src_plane;
            dst_l0_plane_out[flow_idx] = dst_l0_plane;
            src_l1_eps_out[flow_idx] = src_l1_eps;
            dst_l1_plane_out[flow_idx] = dst_l1_plane;
            dst_l1_eps_out[flow_idx] = dst_l1_eps;
        }
    }

    const auto greedy_end = Clock::now();
    *sort_ms_out = std::chrono::duration<double, std::milli>(sort_end - sort_start).count();
    *greedy_ms_out = std::chrono::duration<double, std::milli>(greedy_end - greedy_start).count();
    return 0;
}

extern "C" int lpt_assign_pod_local_core(
    const int32_t* src_ranks,
    const int32_t* dst_ranks,
    const uint64_t* flow_bytes,
    size_t flow_count,
    int nodes,
    int ranks_per_group,
    int ranks_per_tray,
    int l1_planes,
    int l1_eps_per_l1_plane,
    uint64_t* src_l0_load_out,
    uint64_t* dst_l0_load_out,
    size_t l0_count,
    uint64_t* src_l1_load_out,
    uint64_t* dst_l1_load_out,
    size_t l1_count,
    int32_t* src_l0_id_out,
    int32_t* dst_l0_id_out,
    int32_t* src_l1_id_out,
    int32_t* dst_l1_id_out,
    int32_t* src_l0_plane_out,
    int32_t* dst_l0_plane_out,
    int32_t* src_l1_eps_out,
    int32_t* dst_l1_plane_out,
    int32_t* dst_l1_eps_out,
    double* src_sort_ms_out,
    double* src_greedy_ms_out,
    double* dst_sort_ms_out,
    double* dst_greedy_ms_out,
    double* wall_ms_out) {
    if (!src_ranks || !dst_ranks || !flow_bytes ||
        !src_l0_load_out || !dst_l0_load_out || !src_l1_load_out || !dst_l1_load_out ||
        !src_l0_id_out || !dst_l0_id_out || !src_l1_id_out || !dst_l1_id_out ||
        !src_l0_plane_out || !dst_l0_plane_out ||
        !src_l1_eps_out || !dst_l1_plane_out || !dst_l1_eps_out ||
        !src_sort_ms_out || !src_greedy_ms_out ||
        !dst_sort_ms_out || !dst_greedy_ms_out || !wall_ms_out) {
        return 1;
    }
    if (nodes <= 0 || ranks_per_group <= 0 || ranks_per_tray <= 0 ||
        l1_planes <= 0 || l1_eps_per_l1_plane <= 0) {
        return 2;
    }
    if (nodes % ranks_per_group != 0 || nodes % ranks_per_tray != 0 ||
        ranks_per_group % ranks_per_tray != 0) {
        return 3;
    }

    const int groups = nodes / ranks_per_group;
    const int trays_per_group = ranks_per_group / ranks_per_tray;
    const size_t expected_l0_count = static_cast<size_t>(nodes / ranks_per_tray) *
                                     static_cast<size_t>(l1_planes);
    const size_t expected_l1_count = static_cast<size_t>(groups) *
                                     static_cast<size_t>(l1_planes) *
                                     static_cast<size_t>(l1_eps_per_l1_plane);
    if (l0_count < expected_l0_count || l1_count < expected_l1_count) {
        return 4;
    }

    for (size_t i = 0; i < expected_l0_count; ++i) {
        src_l0_load_out[i] = 0;
        dst_l0_load_out[i] = 0;
    }
    for (size_t i = 0; i < expected_l1_count; ++i) {
        src_l1_load_out[i] = 0;
        dst_l1_load_out[i] = 0;
    }

    std::vector<std::vector<size_t>> src_by_group(groups);
    std::vector<std::vector<size_t>> dst_by_group(groups);
    for (size_t i = 0; i < flow_count; ++i) {
        const int32_t src_rank = src_ranks[i];
        const int32_t dst_rank = dst_ranks[i];
        if (src_rank < 0 || src_rank >= nodes) {
            return 5;
        }
        if (dst_rank < 0 || dst_rank >= nodes) {
            return 6;
        }
        src_by_group[src_rank / ranks_per_group].push_back(i);
        dst_by_group[dst_rank / ranks_per_group].push_back(i);
    }

    using Clock = std::chrono::steady_clock;
    const auto wall_start = Clock::now();
    std::vector<double> src_sort_ms(groups, 0.0);
    std::vector<double> src_greedy_ms(groups, 0.0);
    std::vector<double> dst_sort_ms(groups, 0.0);
    std::vector<double> dst_greedy_ms(groups, 0.0);

    std::vector<std::thread> threads;
    threads.reserve(static_cast<size_t>(groups) * 2);

    for (int group = 0; group < groups; ++group) {
        if (!src_by_group[group].empty()) {
            threads.emplace_back([&, group]() {
                const auto sort_start = Clock::now();
                std::vector<size_t> ids = src_by_group[group];
                std::sort(ids.begin(), ids.end(), [&](size_t a, size_t b) {
                    if (flow_bytes[a] != flow_bytes[b]) {
                        return flow_bytes[a] > flow_bytes[b];
                    }
                    return a < b;
                });
                const auto sort_end = Clock::now();
                const auto greedy_start = Clock::now();

                for (size_t flow_idx : ids) {
                    const int32_t src_rank = src_ranks[flow_idx];
                    const int src_tray = src_rank / ranks_per_tray;
                    const int src_l0_base = src_tray * l1_planes;

                    int src_l0_id = src_l0_base;
                    uint64_t src_l0_best_load = src_l0_load_out[src_l0_id];
                    for (int candidate = src_l0_base + 1; candidate < src_l0_base + l1_planes; ++candidate) {
                        const uint64_t candidate_load = src_l0_load_out[candidate];
                        if (candidate_load < src_l0_best_load) {
                            src_l0_id = candidate;
                            src_l0_best_load = candidate_load;
                        }
                    }
                    const int src_plane = src_l0_id - src_l0_base;
                    const int src_l1_base = (group * l1_planes + src_plane) * l1_eps_per_l1_plane;

                    int src_l1_id = src_l1_base;
                    uint64_t src_l1_best_load = src_l1_load_out[src_l1_id];
                    for (int candidate = src_l1_base + 1; candidate < src_l1_base + l1_eps_per_l1_plane; ++candidate) {
                        const uint64_t candidate_load = src_l1_load_out[candidate];
                        if (candidate_load < src_l1_best_load) {
                            src_l1_id = candidate;
                            src_l1_best_load = candidate_load;
                        }
                    }

                    src_l0_load_out[src_l0_id] += flow_bytes[flow_idx];
                    src_l1_load_out[src_l1_id] += flow_bytes[flow_idx];
                    src_l0_id_out[flow_idx] = src_l0_id;
                    src_l1_id_out[flow_idx] = src_l1_id;
                    src_l0_plane_out[flow_idx] = src_plane;
                    src_l1_eps_out[flow_idx] = src_l1_id - src_l1_base;
                }

                const auto greedy_end = Clock::now();
                src_sort_ms[group] = std::chrono::duration<double, std::milli>(sort_end - sort_start).count();
                src_greedy_ms[group] = std::chrono::duration<double, std::milli>(greedy_end - greedy_start).count();
            });
        }

        if (!dst_by_group[group].empty()) {
            threads.emplace_back([&, group]() {
                const auto sort_start = Clock::now();
                std::vector<size_t> ids = dst_by_group[group];
                std::sort(ids.begin(), ids.end(), [&](size_t a, size_t b) {
                    if (flow_bytes[a] != flow_bytes[b]) {
                        return flow_bytes[a] > flow_bytes[b];
                    }
                    return a < b;
                });
                const auto sort_end = Clock::now();
                const auto greedy_start = Clock::now();

                for (size_t flow_idx : ids) {
                    const int32_t dst_rank = dst_ranks[flow_idx];
                    const int dst_tray = dst_rank / ranks_per_tray;
                    const int dst_l0_base = dst_tray * l1_planes;

                    int dst_l0_id = dst_l0_base;
                    uint64_t dst_l0_best_load = dst_l0_load_out[dst_l0_id];
                    for (int candidate = dst_l0_base + 1; candidate < dst_l0_base + l1_planes; ++candidate) {
                        const uint64_t candidate_load = dst_l0_load_out[candidate];
                        if (candidate_load < dst_l0_best_load) {
                            dst_l0_id = candidate;
                            dst_l0_best_load = candidate_load;
                        }
                    }
                    const int dst_l0_plane = dst_l0_id - dst_l0_base;

                    const int dst_l1_base = group * l1_planes * l1_eps_per_l1_plane;
                    const int dst_l1_limit = dst_l1_base + l1_planes * l1_eps_per_l1_plane;
                    int dst_l1_id = dst_l1_base;
                    uint64_t dst_l1_best_load = dst_l1_load_out[dst_l1_id];
                    for (int candidate = dst_l1_base + 1; candidate < dst_l1_limit; ++candidate) {
                        const uint64_t candidate_load = dst_l1_load_out[candidate];
                        if (candidate_load < dst_l1_best_load) {
                            dst_l1_id = candidate;
                            dst_l1_best_load = candidate_load;
                        }
                    }
                    const int dst_l1_local = dst_l1_id - dst_l1_base;

                    dst_l0_load_out[dst_l0_id] += flow_bytes[flow_idx];
                    dst_l1_load_out[dst_l1_id] += flow_bytes[flow_idx];
                    dst_l0_id_out[flow_idx] = dst_l0_id;
                    dst_l1_id_out[flow_idx] = dst_l1_id;
                    dst_l0_plane_out[flow_idx] = dst_l0_plane;
                    dst_l1_plane_out[flow_idx] = dst_l1_local / l1_eps_per_l1_plane;
                    dst_l1_eps_out[flow_idx] = dst_l1_local % l1_eps_per_l1_plane;
                }

                const auto greedy_end = Clock::now();
                dst_sort_ms[group] = std::chrono::duration<double, std::milli>(sort_end - sort_start).count();
                dst_greedy_ms[group] = std::chrono::duration<double, std::milli>(greedy_end - greedy_start).count();
            });
        }
    }

    for (std::thread& t : threads) {
        t.join();
    }

    const auto wall_end = Clock::now();
    double src_sort_sum = 0.0;
    double src_greedy_sum = 0.0;
    double dst_sort_sum = 0.0;
    double dst_greedy_sum = 0.0;
    for (int group = 0; group < groups; ++group) {
        src_sort_sum += src_sort_ms[group];
        src_greedy_sum += src_greedy_ms[group];
        dst_sort_sum += dst_sort_ms[group];
        dst_greedy_sum += dst_greedy_ms[group];
    }

    *src_sort_ms_out = src_sort_sum;
    *src_greedy_ms_out = src_greedy_sum;
    *dst_sort_ms_out = dst_sort_sum;
    *dst_greedy_ms_out = dst_greedy_sum;
    *wall_ms_out = std::chrono::duration<double, std::milli>(wall_end - wall_start).count();
    return 0;
}
