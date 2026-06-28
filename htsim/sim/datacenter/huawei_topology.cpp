// -*- c-basic-offset: 4; indent-tabs-mode: nil -*-
#include "huawei_topology.h"

#include <algorithm>
#include <limits>
#include <sstream>
#include <stdexcept>

using namespace std;

HuaweiTopology::HuaweiTopology(const HuaweiTopologyConfig& cfg, EventList& eventlist)
    : _cfg(cfg), _eventlist(eventlist) {
    validate_config();
    _trays = (_cfg.nodes + _cfg.ranks_per_tray - 1) / _cfg.ranks_per_tray;
    _l0_count = _trays * _cfg.l1_planes;
    _l1_count = _cfg.groups * _cfg.l1_planes * _cfg.l1_eps_per_l1_plane;
    _logical_nodes_per_group = huawei_ocs_coupled_logical_nodes_per_group(
            _cfg.l1_planes, _cfg.l1_eps_per_l1_plane);

    init_switches();
    init_ocs();
}

HuaweiTopology::~HuaweiTopology() {
    for (HuaweiSwitch* sw : _l0_switches) {
        delete sw;
    }
    for (HuaweiSwitch* sw : _l1_switches) {
        delete sw;
    }
    for (auto& it : _links) {
        delete it.second.queue;
        delete it.second.pipe;
    }
}

void HuaweiTopology::validate_config() const {
    if (_cfg.nodes == 0) {
        throw invalid_argument("HuaweiTopology requires positive nodes");
    }
    if (_cfg.groups == 0) {
        throw invalid_argument("HuaweiTopology requires positive groups");
    }
    if (_cfg.ranks_per_tray == 0) {
        throw invalid_argument("HuaweiTopology requires positive ranks_per_tray");
    }
    if (_cfg.ranks_per_group == 0) {
        throw invalid_argument("HuaweiTopology requires positive ranks_per_group");
    }
    if (_cfg.l1_planes == 0) {
        throw invalid_argument("HuaweiTopology requires positive l1_planes");
    }
    if (_cfg.l1_eps_per_l1_plane == 0 || _cfg.l1_eps_per_l1_plane % 2 != 0) {
        throw invalid_argument("HuaweiTopology requires even positive l1_eps_per_l1_plane");
    }
    if (_cfg.groups * _cfg.ranks_per_group < _cfg.nodes) {
        throw invalid_argument("HuaweiTopology groups * ranks_per_group is smaller than nodes");
    }
    if (_cfg.queue_size == 0) {
        throw invalid_argument("HuaweiTopology requires positive queue_size");
    }
}

void HuaweiTopology::init_switches() {
    _l0_switches.resize(_l0_count, nullptr);
    for (uint32_t l0 = 0; l0 < _l0_count; l0++) {
        _l0_switches[l0] = new HuaweiSwitch(
                _eventlist, "Huawei_L0_" + to_string(l0), HuaweiSwitch::L0, l0,
                _cfg.switch_latency);
    }

    _l1_switches.resize(_l1_count, nullptr);
    for (uint32_t l1 = 0; l1 < _l1_count; l1++) {
        _l1_switches[l1] = new HuaweiSwitch(
                _eventlist, "Huawei_L1_" + to_string(l1), HuaweiSwitch::L1, l1,
                _cfg.switch_latency);
        _l1_switches[l1]->set_special_next_hop_resolver(
                HuaweiTopology::l1_special_next_hop_thunk, this);
    }
}

void HuaweiTopology::init_ocs() {
    if (_cfg.ocs_mode == HuaweiOcsMode::OFF) {
        return;
    }
    _ocs_graph = build_huawei_ocs_coupled_template(
            _cfg.groups,
            _cfg.l1_planes,
            _cfg.l1_eps_per_l1_plane,
            _cfg.ocs_degree,
            _cfg.ocs_seed);

    if (_cfg.ocs_mode == HuaweiOcsMode::SPRAYPOINT) {
        HuaweiOcsSprayPointParams params;
        params.spray_p = _cfg.spray_p;
        params.spray_h = _cfg.spray_h;
        params.spray_levels = _cfg.spray_levels;
        params.spray_seed = _cfg.ocs_seed;
        _spraypoint = make_unique<HuaweiOcsSprayPointRouter>(
                _ocs_graph, _cfg.groups, _logical_nodes_per_group, params);
    } else if (_cfg.ocs_mode == HuaweiOcsMode::KSP) {
        HuaweiOcsKspParams params;
        params.k = _cfg.ksp_k;
        params.max_hops = _cfg.ksp_max_hops;
        params.seed = _cfg.ksp_seed;
        params.max_paths_per_pair = _cfg.ksp_max_paths_per_pair;
        _ksp = make_unique<HuaweiOcsKspRouter>(
                _ocs_graph, _cfg.groups, _logical_nodes_per_group, params);
    }
}

vector<const Route*>* HuaweiTopology::get_bidir_paths(uint32_t src, uint32_t dest, bool reverse) {
    (void)src;
    (void)dest;
    (void)reverse;
    throw logic_error("HuaweiTopology uses switch-local FIB and does not enumerate paths");
}

vector<uint32_t>* HuaweiTopology::get_neighbours(uint32_t src) {
    (void)src;
    throw logic_error("HuaweiTopology::get_neighbours is not implemented for physical switches yet");
}

uint32_t HuaweiTopology::rank_group(uint32_t rank) const {
    if (rank >= _cfg.nodes) {
        throw out_of_range("Huawei rank out of range");
    }
    return rank / _cfg.ranks_per_group;
}

uint32_t HuaweiTopology::rank_tray(uint32_t rank) const {
    if (rank >= _cfg.nodes) {
        throw out_of_range("Huawei rank out of range");
    }
    return rank / _cfg.ranks_per_tray;
}

uint32_t HuaweiTopology::rank_l0(uint32_t rank, uint32_t plane) const {
    if (plane >= _cfg.l1_planes) {
        throw out_of_range("Huawei plane out of range");
    }
    return rank_tray(rank) * _cfg.l1_planes + plane;
}

uint32_t HuaweiTopology::l1_physical_id(uint32_t group, uint32_t plane, uint32_t eps) const {
    return huawei_ocs_coupled_endpoint_id(
            group, plane, eps, _cfg.l1_planes, _cfg.l1_eps_per_l1_plane);
}

uint32_t HuaweiTopology::l1_group(uint32_t l1_id) const {
    return huawei_ocs_decode_coupled_endpoint(
            l1_id, _cfg.l1_planes, _cfg.l1_eps_per_l1_plane).group;
}

uint32_t HuaweiTopology::l1_plane(uint32_t l1_id) const {
    return huawei_ocs_decode_coupled_endpoint(
            l1_id, _cfg.l1_planes, _cfg.l1_eps_per_l1_plane).l1_plane;
}

uint32_t HuaweiTopology::l1_eps(uint32_t l1_id) const {
    return huawei_ocs_decode_coupled_endpoint(
            l1_id, _cfg.l1_planes, _cfg.l1_eps_per_l1_plane).l1_eps;
}

uint32_t HuaweiTopology::l1_logical_node(uint32_t l1_id) const {
    HuaweiOcsEndpoint ep = huawei_ocs_decode_coupled_endpoint(
            l1_id, _cfg.l1_planes, _cfg.l1_eps_per_l1_plane);
    return huawei_ocs_coupled_logical_node_id(
            ep.group, ep.l1_plane, ep.coupled_pair,
            _cfg.l1_planes, _cfg.l1_eps_per_l1_plane);
}

uint32_t HuaweiTopology::l1_from_logical_node(uint32_t logical_node, uint32_t coupled_member) const {
    HuaweiOcsLogicalNode node = huawei_ocs_decode_coupled_logical_node(
            logical_node, _cfg.l1_planes, _cfg.l1_eps_per_l1_plane);
    uint32_t eps = coupled_member == 0 ? node.l1_eps_member0 : node.l1_eps_member1;
    return l1_physical_id(node.group, node.l1_plane, eps);
}

void HuaweiTopology::connect_endpoints(
        uint32_t src,
        uint32_t dst,
        UecSrc& uec_src,
        UecSink& uec_snk,
        simtime_picosec start_time) {
    bool same_tray = src != dst && rank_tray(src) == rank_tray(dst);
    if (same_tray) {
        Route* routeout = make_local_route(src, dst, 0, uec_snk.getPort(0));
        Route* routeback = make_local_route(dst, src, 0, uec_src.getPort(0));
        routeout->set_reverse(routeback);
        routeback->set_reverse(routeout);
        for (uint32_t p = 0; p < _cfg.l1_planes; p++) {
            uec_src.connectPort(p, *routeout, *routeback, uec_snk, start_time);
        }
        return;
    }

    for (uint32_t p = 0; p < _cfg.l1_planes; p++) {
        uint32_t src_l0 = rank_l0(src, p);
        uint32_t dst_l0 = rank_l0(dst, p);

        Route* routeout = make_initial_route(src, src_l0, p);
        Route* routeback = make_initial_route(dst, dst_l0, p);
        routeout->set_reverse(routeback);
        routeback->set_reverse(routeout);
        uec_src.connectPort(p, *routeout, *routeback, uec_snk, start_time);

        install_l0_host_route(dst_l0, dst, uec_src.flowId(), p, uec_snk.getPort(p));
        install_l0_host_route(src_l0, src, uec_snk.flowId(), p, uec_src.getPort(p));

        install_l0_up_routes(src_l0, dst);
        install_l0_up_routes(dst_l0, src);
    }

    install_l1_down_routes(dst);
    install_l1_down_routes(src);
}

Route* HuaweiTopology::make_initial_route(uint32_t rank, uint32_t l0, uint32_t plane) {
    return make_switch_route(
            host_src_name(rank),
            l0_name(l0),
            plane,
            _cfg.external_linkspeed,
            _cfg.link_latency,
            nullptr,
            _l0_switches.at(l0));
}

Route* HuaweiTopology::make_local_route(uint32_t src, uint32_t dst, uint32_t bundle, PacketSink* final_sink) {
    CachedLink& link = get_or_create_link(
            "LOCAL_" + host_src_name(src),
            host_dst_name(dst),
            bundle,
            _cfg.local_linkspeed,
            _cfg.local_latency,
            nullptr,
            nullptr);
    Route* route = new Route();
    route->push_back(link.queue);
    route->push_back(link.pipe);
    route->push_back(final_sink);
    return route;
}

Route* HuaweiTopology::make_l0_to_host_route(
        uint32_t l0,
        uint32_t rank,
        uint32_t plane,
        PacketSink* final_sink) {
    CachedLink& link = get_or_create_link(
            l0_name(l0),
            host_dst_name(rank),
            plane,
            _cfg.external_linkspeed,
            _cfg.link_latency,
            _l0_switches.at(l0),
            nullptr);
    Route* route = new Route();
    route->push_back(link.queue);
    route->push_back(link.pipe);
    route->push_back(final_sink);
    return route;
}

Route* HuaweiTopology::make_switch_route(
        const string& src_name,
        const string& dst_name,
        uint32_t bundle,
        linkspeed_bps speed,
        simtime_picosec latency,
        HuaweiSwitch* src_switch,
        PacketSink* dst_sink) {
    CachedLink& link = get_or_create_link(
            src_name, dst_name, bundle, speed, latency, src_switch, dst_sink);
    Route* route = new Route();
    route->push_back(link.queue);
    route->push_back(link.pipe);
    route->push_back(dst_sink);
    return route;
}

HuaweiTopology::CachedLink& HuaweiTopology::get_or_create_link(
        const string& src_name,
        const string& dst_name,
        uint32_t bundle,
        linkspeed_bps speed,
        simtime_picosec latency,
        HuaweiSwitch* src_switch,
        PacketSink* remote_endpoint) {
    string key = src_name + "->" + dst_name + "(" + to_string(bundle) + ")";
    auto it = _links.find(key);
    if (it != _links.end()) {
        return it->second;
    }

    CachedLink link;
    link.queue = new Queue(speed, _cfg.queue_size, _eventlist, nullptr);
    link.queue->setName(key);
    if (remote_endpoint) {
        link.queue->setRemoteEndpoint(remote_endpoint);
    }
    if (src_switch) {
        src_switch->addPort(link.queue);
    }
    link.pipe = new Pipe(latency, _eventlist);
    link.pipe->setName("Pipe-" + key);

    auto inserted = _links.emplace(key, link);
    return inserted.first->second;
}

void HuaweiTopology::install_l0_host_route(
        uint32_t l0,
        uint32_t dst_rank,
        int flowid,
        uint32_t plane,
        PacketSink* final_sink) {
    string key = host_route_key(l0, dst_rank, flowid);
    if (!_installed_host_routes.insert(key).second) {
        return;
    }
    Route* route = make_l0_to_host_route(l0, dst_rank, plane, final_sink);
    _l0_switches.at(l0)->addHostRoute(dst_rank, flowid, route);
}

void HuaweiTopology::install_l0_up_routes(uint32_t l0, uint32_t dst_rank) {
    uint32_t plane = l0 % _cfg.l1_planes;
    uint32_t tray = l0 / _cfg.l1_planes;
    uint32_t src_group = (tray * _cfg.ranks_per_tray) / _cfg.ranks_per_group;
    if (src_group >= _cfg.groups) {
        throw out_of_range("Huawei L0 group out of range");
    }

    for (uint32_t eps = 0; eps < _cfg.l1_eps_per_l1_plane; eps++) {
        uint32_t l1 = l1_physical_id(src_group, plane, eps);
        string key = route_key("l0up", l0, dst_rank, l1);
        if (!_installed_routes.insert(key).second) {
            continue;
        }
        Route* route = make_switch_route(
                l0_name(l0), l1_name(l1), eps,
                _cfg.external_linkspeed, _cfg.link_latency,
                _l0_switches.at(l0), _l1_switches.at(l1));
        _l0_switches.at(l0)->addRoute(dst_rank, route, UP);
    }
}

void HuaweiTopology::install_l1_down_routes(uint32_t dst_rank) {
    uint32_t group = rank_group(dst_rank);
    for (uint32_t plane = 0; plane < _cfg.l1_planes; plane++) {
        uint32_t dst_l0 = rank_l0(dst_rank, plane);
        for (uint32_t eps = 0; eps < _cfg.l1_eps_per_l1_plane; eps++) {
            uint32_t l1 = l1_physical_id(group, plane, eps);
            string key = route_key("l1down", l1, dst_rank, dst_l0);
            if (!_installed_routes.insert(key).second) {
                continue;
            }
            Route* route = make_switch_route(
                    l1_name(l1), l0_name(dst_l0), eps,
                    _cfg.external_linkspeed, _cfg.link_latency,
                    _l1_switches.at(l1), _l0_switches.at(dst_l0));
            _l1_switches.at(l1)->addRoute(dst_rank, route, DOWN);
        }
    }
}

Route* HuaweiTopology::l1_special_next_hop_thunk(HuaweiSwitch* sw, Packet& pkt, void* context) {
    return static_cast<HuaweiTopology*>(context)->l1_special_next_hop(sw, pkt);
}

Route* HuaweiTopology::l1_special_next_hop(HuaweiSwitch* sw, Packet& pkt) {
    if (sw->getType() != HuaweiSwitch::L1 || _cfg.ocs_mode == HuaweiOcsMode::OFF) {
        return nullptr;
    }

    uint32_t current_l1 = sw->getID();
    uint32_t current_group = l1_group(current_l1);
    uint32_t dst_group = rank_group(pkt.dst());
    if (current_group == dst_group) {
        if (pkt.has_ocs_ksp_route()) {
            pkt.clear_ocs_ksp_route();
        }
        return nullptr;
    }

    uint32_t current_node = l1_logical_node(current_l1);
    uint32_t current_member = l1_eps(current_l1) % 2;
    uint32_t next_node = numeric_limits<uint32_t>::max();

    if (_cfg.ocs_mode == HuaweiOcsMode::SPRAYPOINT) {
        if (!_spraypoint) {
            throw logic_error("Huawei SprayPoint router is not initialized");
        }
        bool source_step = !pkt.ocs_source_sprayed()
                && pkt.src() < _cfg.nodes
                && current_group == rank_group(pkt.src());
        if (source_step) {
            pkt.mark_ocs_source_sprayed();
        }
        HuaweiOcsSprayPointChoice choice =
                _cfg.ocs_choice == HuaweiOcsChoice::FLOW_HASH
                        ? HuaweiOcsSprayPointChoice::FLOW_HASH
                        : HuaweiOcsSprayPointChoice::PACKET_RR;
        uint32_t rr = _cfg.ocs_choice == HuaweiOcsChoice::FLOW_HASH ? 0 : sw->next_special_rr();
        next_node = _spraypoint->choose_next_hop(
                current_node, dst_group, source_step,
                pkt.flow_id(), pkt.pathid(), rr, choice);
    } else if (_cfg.ocs_mode == HuaweiOcsMode::KSP) {
        if (!_ksp) {
            throw logic_error("Huawei KSP router is not initialized");
        }
        uint32_t src_node = current_node;
        uint32_t path_id = numeric_limits<uint32_t>::max();
        if (pkt.has_ocs_ksp_route()) {
            src_node = pkt.ocs_ksp_src_node();
            if (pkt.ocs_ksp_dst_group() != dst_group) {
                throw logic_error("Huawei KSP packet dst_group metadata mismatch");
            }
            path_id = pkt.ocs_ksp_path_id();
        } else {
            HuaweiOcsKspChoice choice =
                    _cfg.ocs_choice == HuaweiOcsChoice::FLOW_HASH
                            ? HuaweiOcsKspChoice::FLOW_HASH
                            : HuaweiOcsKspChoice::PACKET_RR;
            uint32_t rr = _cfg.ocs_choice == HuaweiOcsChoice::FLOW_HASH ? 0 : sw->next_special_rr();
            path_id = _ksp->choose_path(
                    src_node, dst_group, pkt.flow_id(), pkt.pathid(), pkt.id(), rr, choice);
            if (path_id == numeric_limits<uint32_t>::max()) {
                throw runtime_error("Huawei KSP could not select a path");
            }
            pkt.set_ocs_ksp_route(src_node, dst_group, path_id);
        }
        next_node = _ksp->next_hop(src_node, dst_group, path_id, current_node);
    }

    if (next_node == numeric_limits<uint32_t>::max()) {
        throw runtime_error("Huawei OCS route ended before reaching destination group");
    }

    pkt.set_direction(UP);
    uint32_t next_l1 = l1_from_logical_node(next_node, current_member);
    return l1_to_l1_route(current_l1, next_l1, 0);
}

Route* HuaweiTopology::l1_to_l1_route(uint32_t src_l1, uint32_t dst_l1, uint32_t bundle) {
    return make_switch_route(
            l1_name(src_l1), l1_name(dst_l1), bundle,
            _cfg.external_linkspeed, _cfg.link_latency,
            _l1_switches.at(src_l1), _l1_switches.at(dst_l1));
}

string HuaweiTopology::route_key(const string& prefix, uint32_t sw, uint32_t dst, uint32_t next) const {
    return prefix + ":" + to_string(sw) + ":" + to_string(dst) + ":" + to_string(next);
}

string HuaweiTopology::host_route_key(uint32_t l0, uint32_t dst, int flowid) const {
    return to_string(l0) + ":" + to_string(dst) + ":" + to_string(flowid);
}

string HuaweiTopology::host_src_name(uint32_t rank) {
    return "SRC" + to_string(rank);
}

string HuaweiTopology::host_dst_name(uint32_t rank) {
    return "DST" + to_string(rank);
}

string HuaweiTopology::l0_name(uint32_t l0) {
    return "L0_" + to_string(l0);
}

string HuaweiTopology::l1_name(uint32_t l1) {
    return "L1_" + to_string(l1);
}
