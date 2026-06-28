// -*- c-basic-offset: 4; indent-tabs-mode: nil -*-
#ifndef HUAWEI_OCS_GRAPH_H
#define HUAWEI_OCS_GRAPH_H

#include <cstdint>
#include <string>
#include <utility>
#include <vector>

struct HuaweiOcsGraph {
    uint32_t nodes = 0;
    uint32_t degree = 0;
    uint32_t seed = 42;
    std::vector<std::pair<uint32_t, uint32_t>> edges;
    std::vector<uint32_t> edge_ocs;
    std::vector<std::vector<uint32_t>> adjacency;

    std::vector<uint32_t> degrees() const;
    bool is_connected() const;
};

struct HuaweiOcsEndpoint {
    uint32_t group = 0;
    uint32_t l1_plane = 0;
    uint32_t l1_eps = 0;
    uint32_t endpoint_index = 0;
    uint32_t coupled_pair = 0;
    uint32_t coupled_member = 0;
};

struct HuaweiOcsLogicalNode {
    uint32_t group = 0;
    uint32_t l1_plane = 0;
    uint32_t coupled_pair = 0;
    uint32_t local_index = 0;
    uint32_t l1_eps_member0 = 0;
    uint32_t l1_eps_member1 = 0;
};

uint32_t huawei_ocs_template_node_id(uint32_t group, uint32_t eps, uint32_t l1_eps_per_plane);
std::pair<uint32_t, uint32_t> huawei_ocs_decode_template_node(
        uint32_t node,
        uint32_t l1_eps_per_plane);
uint32_t huawei_ocs_coupled_endpoint_id(
        uint32_t group,
        uint32_t l1_plane,
        uint32_t l1_eps,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);
HuaweiOcsEndpoint huawei_ocs_decode_coupled_endpoint(
        uint32_t node,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);
uint32_t huawei_ocs_coupled_logical_node_id(
        uint32_t group,
        uint32_t l1_plane,
        uint32_t coupled_pair,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);
HuaweiOcsLogicalNode huawei_ocs_decode_coupled_logical_node(
        uint32_t node,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);
uint32_t huawei_ocs_coupled_logical_nodes_per_group(
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);

HuaweiOcsGraph build_huawei_ocs_expander(
        uint32_t nodes,
        uint32_t degree,
        uint32_t ocs_expander_seed = 42,
        uint32_t max_attempts = 10000);

HuaweiOcsGraph build_huawei_ocs_coupled_template(
        uint32_t groups,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane,
        uint32_t degree,
        uint32_t ocs_expander_seed = 42,
        uint32_t max_attempts = 10000);

bool huawei_ocs_coupled_template_has_pair_copy(
        const HuaweiOcsGraph& graph,
        uint32_t groups,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);
bool huawei_ocs_coupled_template_has_logical_nodes(
        const HuaweiOcsGraph& graph,
        uint32_t groups,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane);

void write_huawei_ocs_edges_csv(const HuaweiOcsGraph& graph, const std::string& path);
void write_huawei_ocs_coupled_edges_csv(
        const HuaweiOcsGraph& graph,
        uint32_t groups,
        uint32_t l1_planes,
        uint32_t l1_eps_per_l1_plane,
        const std::string& path);

#endif
