# Huawei 拓扑仿真支持开发指南

这份文档用于指导 HTSIM 增加 Huawei 风格多平面拓扑的仿真能力。目标是补齐拓扑、端口、路由、链路统计这些仿真功能，不在这里做新的端侧调度算法。

## 目标语义

目标拓扑参考两个来源：

- `Rail-MoE/scripts/huawei_topo.py`
- `需求/组网方案-专家数据-问题约束_v3.pdf`

在这个模型里，rank 等价为一个 XPU/GPU 仿真端点。一个服务器/tray 内有多个 rank，这些 rank 在服务器内部全连接。服务器外部是多 plane 网络：每个 rank 有一个外部端口接入一个 plane，也就是 rank 的 `port p` 连接到 `L0 plane p`。

当前 `huawei_topo.py` 已经明确了这些层级：

- `gpu`：rank/XPU 端点。
- `tray_mesh`：同一个 tray 内 XPU 两两直连。
- `xpu_l0`：每个 XPU 按 plane 连接到对应 L0 EPS。
- `l0_l1`：L0 到同 plane 的 L1 EPS。
- `l1_l2`：L1 到 L2 electric，L2 是合平面/汇聚层。

对于 L2-OCS 模式，后续文档以 `02_l2_ocs_design.md` 和 `05_8192_cluster_topology.md` 的修订口径为准：

```text
PDF 第 3 页的 L1 EPS 总数按 512M/N 修订
OCS 物理台数仍为 8N
每台 OCS 端口仍为 512M/N
每台 L1 EPS 上行仍为 8N
L1 每两个 EPS 同号口合并后，OCS 路由运行在 logical_node(group, l1_plane, coupled_pair) 上
```

默认 L2-OCS 拓扑不是随机 L1 endpoint 图，而是在合口逻辑节点上按跨组 degree 构造：

```text
logical_nodes = 256M/N
logical_nodes_per_group = 2M
OcsEffectiveDegree = min(8N, 256M/N - 2M)

if OcsEffectiveDegree == 256M/N - 2M:
  OcsTopology complete_cross_group
else:
  OcsTopology sparse_expander
```

当 `8N` 不足以覆盖跨组全连接，或需要研究稀疏 OCS 图时，使用 sparse expander；SprayPoint / KSP 多跳路由主要在这个非全连接场景下发挥作用。当前 8192 卡功能测试口径是 `N=8, M=4`，对应 `logical_nodes=128`、`logical_nodes_per_group=8`、`OcsEffectiveDegree=64`，因此是 `sparse_expander`，不是全连接。

核心可调参数：

- `groups`：L1/L2 group 数量。
- `l0_per_group`：每个 group 下的 L0 domain 数。
- `trays_per_l0`：每个 L0 domain 下的服务器/tray 数。
- `gpus_per_tray`：每个服务器内的 rank/XPU 数。
- `planes`：并行 L0/L1 plane 数，同时也是每个 rank 的外部端口数。
- `l1_eps_per_l1_plane`：每个 group、每个 L1 plane 内的 L1 EPS 数。
- `l0_uplinks_per_eps`：每个 L0 EPS 连接几个同 plane L1 EPS。
- `l2_switches`：L2 electric 交换机数量。
- `l2_mode`：`eps` 或 `ocs`。
- `ocs_topology`：`complete_cross_group` 或 `sparse_expander`。
- `ocs_effective_degree`：合口逻辑 OCS 图的实际跨组 degree，默认 `min(8N, 256M/N - 2M)`。

## 现有 HTSIM 缺口

当前可运行链路主要是 `main_uec.cpp` 加 `FatTreeTopology`。

已经可用的能力：

- `UecNIC` 支持多个 port，会从空闲 port 中轮询选择，并调用 `src.getPortRoute(portnum)`。
- `UecSrc::connectPort(port_num, routeout, routeback, sink, start)` 可以给每个 port 绑定独立路由。
- switch 侧 ECMP 已经在 `FatTreeSwitch::getNextHop()` 中实现。
- `ecmp_rr` 对数据包是逐包 round-robin；小控制包仍然走 hash。
- 我们已经有 `link_load_sampler`，可以按 link 采样吞吐和队列。

缺的能力：

- `FatTreeTopology` 明确不支持 host 到 ToR 的 bundle，host 链路固定只用 bundle `0`。
- `main_uec.cpp` 里的 `-planes` 当前会创建多个 fat-tree 实例，但 `planes != 1` 会触发 assert，所以不能直接用。
- `ports` 只是 UEC NIC 内部的发送并行度，不等价于物理多 NIC。只有当 topology 给每个 port 绑定不同物理路径时，它才表示多 plane 端口。
- 服务器内部 rank 全连接目前没建模。fat-tree 中即使同服务器/同 ToR，流量也会走 `src -> ToR -> dst`。
- fat-tree 路由依赖 pod/tier 公式，不能表达 Huawei 的 L0/L1/L2 plane 结构。

## 设计选择

建议新增 Huawei 专用拓扑，不要强行改造 `FatTreeTopology`。

建议新增文件：

- `htsim/sim/datacenter/huawei_topology.h`
- `htsim/sim/datacenter/huawei_topology.cpp`
- `htsim/sim/datacenter/huawei_switch.h`
- `htsim/sim/datacenter/huawei_switch.cpp`

这样可以保持 fat-tree 路径稳定，只在公共接口和 `main_uec.cpp` 中增加少量拓扑选择逻辑。

## 拓扑配置格式

第一版建议先做参数化配置，不急着支持任意 adjacency。因为 Huawei 拓扑有明确层级，用参数生成更不容易出错。

示例：

```text
Topology Huawei
Ranks 256
Groups 4
L0PerGroup 4
TraysPerL0 2
RanksPerTray 8
Planes 2
L1EpsPerPlane 2
L0UplinksPerEps 2
L2Switches 8

LinkSpeedRankLocalGbps 900
LinkSpeedRankL0Gbps 100
LinkSpeedL0L1Gbps 100
LinkSpeedL1L2Gbps 100

LatencyRankLocalNs 200
LatencyRankL0Ns 1000
LatencyL0L1Ns 1000
LatencyL1L2Ns 1000
SwitchLatencyNs 0
```

rank 映射公式：

```text
rank_id = (((group * l0_per_group + l0) * trays_per_l0 + tray) * gpus_per_tray + xpu)
server_id = rank_id / gpus_per_tray
plane = port_id
```

节点 ID 顺序建议固定为：

```text
rank -> L0 -> L1 -> L2
```

并输出 `huawei_idmap.txt`，方便后续检查 link load 和 flow 结果。

## 链路模型

每条有方向的物理链路都按照 HTSIM 既有方式建成 queue + pipe。

建议链路类型：

- `rank_local`：同服务器 rank 直连。
- `rank_l0_up` / `rank_l0_down`：rank 到 L0。
- `l0_l1_up` / `l0_l1_down`：L0 到 L1。
- `l1_l2_up` / `l1_l2_down`：L1 到 L2。

同服务器通信应该直接走本地链路：

```text
rank_src -> local_link -> rank_dst
```

这样服务器内部通信不会占用 L0/L1/L2 网络带宽。

## UEC Port 绑定

外部通信时，一个 UEC port 对应一个 plane。

连接 setup 时：

- 如果 `src` 和 `dst` 在同一个服务器/tray：
  - 只绑定 `port 0` 到服务器内部直连 route。
  - 不注册外部 switch host route。
- 如果 `src` 和 `dst` 不在同一个服务器/tray：
  - 对 `p in [0, planes)` 循环。
  - `routeout` 从 `rank(src) -> L0(src, plane p)` 开始。
  - `routeback` 从 `rank(dst) -> L0(dst, plane p)` 开始。
  - 调用 `uec_src->connectPort(p, routeout, routeback, sink, start_time)`。
  - 在对应 plane 的源 L0 和目的 L0 上注册 host delivery。

这一步是核心：UEC 已经有多 port 发送能力，我们要补的是“每个 port 确实连到不同物理 plane”。

## 路由模型

不要预计算所有端到端 path。5,000 rank 时，如果枚举 `src-l0-l1-l2-l1-l0-dst` 的组合，路径数量会爆炸。

建议照 `FatTreeSwitch::getNextHop()` 做 switch-local FIB：

- L0 switch：
  - 如果目的 rank 在当前 L0/tray/plane 下，向下转发到 rank。
  - 否则向上转发到同 plane 的 L1 候选集合。
- L1 switch：
  - 如果目的 group 是本 group，且存在同 plane 下行 L0 候选，则向下转发。
  - 否则向上转发到 L2 候选集合。
- L2 switch：
  - 向目的 group 的 L1 候选集合转发。

每个 switch 对每个 destination 只懒加载缓存下一跳集合，而不是缓存完整 end-to-end path。复杂度和活跃 switch-destination 对以及本地 fanout 相关，不和全局路径组合数直接绑定。

ECMP 策略可以复用当前语义：

- `ecmp_host`：按 `flow_id/path_id` hash。
- `ecmp_rr`：对数据包逐包 round-robin。
- `ecmp_ar`：按队列/利用率做 adaptive routing。

`HuaweiSwitch` 可以复用 `FatTreeSwitch` 的选择逻辑，但不能依赖 fat-tree 的 pod 公式。

## main_uec.cpp 改造点

增加拓扑选择参数：

```text
-topo huawei
-topo_file huawei_256p2.topo
```

建议实现方式：

- 默认仍然走现有 fat-tree。
- 当 `-topo huawei` 时加载 `HuaweiTopologyCfg`。
- 创建一个 `HuaweiTopology`，不要创建 `vector<FatTreeTopology> topo[planes]`。
- `ports = cfg.planes()`。
- 把当前 fat-tree 专属的连接 setup 抽出一个接口：

```cpp
topology->connect_endpoints(src, dst, *uec_src, *uec_snk, start_time);
```

为了少侵入，第一版也可以先在 `main_uec.cpp` 中写一个独立分支：

```cpp
if (topology_kind == HUAWEI) {
    huawei_topology->connect_endpoints(...);
} else {
    // 原 fat-tree 逻辑
}
```

## 统计和调试

link load 采样需要识别 Huawei 链路层级：

- `rank_local`
- `rank_l0 up/down`
- `l0_l1 up/down`
- `l1_l2 up/down`

建议输出：

- `huawei_idmap.txt`：rank 和 switch 的数字 ID、层级、group、l0、tray、plane 等属性。
- `huawei_link_info.csv`：link ID、name、layer、direction、src、dst、plane、speed。
- `huawei_route_sanity.txt`：
  - 同服务器路径只走 `rank_local`。
  - 同 L0 不同服务器路径走某个 plane 的 `rank -> L0 -> ... -> L0 -> rank`。
  - 跨 group 路径形态是 `rank -> L0 -> L1 -> L2 -> L1 -> L0 -> rank`。
  - 每跳 ECMP 候选数量和配置一致。

## 验证计划

从小拓扑开始：

1. `groups=1, l0_per_group=1, trays_per_l0=1, gpus_per_tray=4, planes=2`
   - 只验证同服务器内部直连。
2. `groups=1, l0_per_group=1, trays_per_l0=2, gpus_per_tray=4, planes=2`
   - 验证外部流量能走两个独立 port/plane。
3. `groups=2, l0_per_group=2, trays_per_l0=2, gpus_per_tray=8, planes=2`
   - 对齐当前 `huawei_topo.py` 的可视化测试规模。

每个 case 检查：

- `flowsInfo.csv` 能正常完成。
- `link_load_1ms.csv` 只在预期链路层出现负载。
- `ecmp_host` 和 `ecmp_rr` 下 fabric 负载分布有符合预期的差异。

## 开发里程碑

0. 先补齐仿真器多 plane 端口能力。
   - 现状：Huawei OCS 模式使用 `HuaweiTopology`，`l1_planes` 同时决定 rank 外部端口数。
   - 语义：源端初始 route 只到对应 plane 的 L0；L0/L1 后续由 `HuaweiSwitch` 本地 FIB 决策。
   - 验证脚本：`tests/functional/run_huawei_switch_local_smoke.sh` 和 `tests/functional/run_huawei_ocs_dataplane_tests.sh`。
1. 增加同服务器 rank 直连 route。
   - 现状：`main_uec.cpp` 支持 `-local_tray_size N`。
   - 语义：rank ID 按连续区间划分 tray；`src / N == dst / N` 时走本地 direct route，否则继续走原来的外部网络。
   - 可选参数：`-local_linkspeed Mbps`、`-local_latency_ns ns`。
   - 当前 smoke 包含 1 条本地流和 4 条外部流。
2. 增加 `HuaweiTopologyCfg` 参数解析和 idmap 输出。
3. 增加 Huawei 链路分配：queue、pipe、switch、endpoint。
4. 增加 plane-aware UEC port 绑定。
5. 增加 `HuaweiSwitch::getNextHop()`，采用 lazy FIB。
6. `main_uec.cpp` 在 `-huawei_ocs_mode spraypoint|ksp` 时进入 Huawei switch-local topology。
7. 在 `tests/functional/` 下保留当前 Huawei smoke / dataplane run。
8. 扩展 plotting，让 link-load 图识别 Huawei 层级。

## 非目标

- 不在这里加入新的端侧调度/拥塞算法。
- 不预计算所有 source-destination path。
- 不把一个服务器内所有 rank 合并成一个 HTSIM host；rank 仍然是仿真端点。
- 不直接复用当前 `-planes` 逻辑；它目前只是半成品，不能表达物理多 plane NIC。

## 第一阶段功能验证命令

运行 Huawei switch-local smoke：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_huawei_switch_local_smoke.sh
```

运行 M=4,N=8 OCS 功能测试：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

当前 smoke 观察：

- `planes=2`：5 条流完成，其中 1 条同 tray 直连，4 条走外部网络。
- 日志中会打印 `Local tray direct flows 1 / 5`。
- `link_info.csv` 中会出现 `LOCAL_DATA_*` 和 `LOCAL_ACK_*` 队列；当前 link-load 分类暂时显示为 `unknown`。
- `planes=2` 下外部链路会按两个独立 plane 建立，`LOCAL_*` 链路不会注册到 ToR switch。
