// -*- c-basic-offset: 4; indent-tabs-mode: nil -*-
#ifndef HUAWEI_OCS_KSP_H
#define HUAWEI_OCS_KSP_H

#include "huawei_ocs_graph.h"

#include <cstdint>
#include <string>
#include <vector>

struct HuaweiOcsKspParams {
    uint32_t k = 8;
    uint32_t max_hops = 0;  // 0 means graph.nodes - 1.
    uint32_t seed = 42;
    uint32_t max_paths_per_pair = 100000;
};

enum class HuaweiOcsKspChoice {
    FLOW_HASH,
    PACKET_HASH,
    PACKET_RR,
};

struct HuaweiOcsKspPath {
    uint32_t src_node = 0;
    uint32_t src_group = 0;
    uint32_t src_eps = 0;
    uint32_t dst_group = 0;
    uint32_t path_id = 0;
    std::vector<uint32_t> nodes;
};

class HuaweiOcsKspRouter {
public:
    HuaweiOcsKspRouter(
            const HuaweiOcsGraph& graph,
            uint32_t groups,
            uint32_t l1_eps_per_plane,
            HuaweiOcsKspParams params);

    const HuaweiOcsGraph& graph() const { return _graph; }
    uint32_t groups() const { return _groups; }
    uint32_t l1_eps_per_plane() const { return _l1_eps_per_plane; }
    const HuaweiOcsKspParams& params() const { return _params; }
    uint32_t effective_max_hops() const { return _effective_max_hops; }

    const std::vector<HuaweiOcsKspPath>& paths(uint32_t src_node, uint32_t dst_group) const;
    bool is_dst_group(uint32_t template_node, uint32_t dst_group) const;

    uint32_t choose_path(
            uint32_t src_node,
            uint32_t dst_group,
            uint32_t flow_id,
            uint32_t path_id,
            uint32_t packet_id,
            uint32_t rr_counter,
            HuaweiOcsKspChoice choice) const;

    uint32_t next_hop(
            uint32_t src_node,
            uint32_t dst_group,
            uint32_t ksp_path_id,
            uint32_t current_node) const;

    uint64_t total_paths() const;
    uint32_t min_path_count() const;
    uint32_t max_path_count() const;

private:
    HuaweiOcsGraph _graph;
    uint32_t _groups;
    uint32_t _l1_eps_per_plane;
    HuaweiOcsKspParams _params;
    uint32_t _effective_max_hops;
    std::vector<std::vector<HuaweiOcsKspPath>> _paths;

    size_t index(uint32_t src_node, uint32_t dst_group) const;
    std::vector<HuaweiOcsKspPath> build_paths(uint32_t src_node, uint32_t dst_group) const;
    std::vector<uint32_t> shortest_path_to_group(
            uint32_t start_node,
            uint32_t dst_group,
            const std::vector<uint8_t>& banned_nodes,
            const std::vector<std::pair<uint32_t, uint32_t>>& banned_edges) const;
    std::vector<HuaweiOcsKspPath> yen_paths(uint32_t src_node, uint32_t dst_group) const;
    uint64_t rank_key(const std::vector<uint32_t>& path, uint32_t src_node, uint32_t dst_group) const;
    uint64_t choice_key(
            uint32_t src_node,
            uint32_t dst_group,
            uint32_t flow_id,
            uint32_t path_id,
            uint32_t packet_id) const;
};

void write_huawei_ocs_ksp_paths_csv(
        const HuaweiOcsKspRouter& router,
        const std::string& path);

#endif
