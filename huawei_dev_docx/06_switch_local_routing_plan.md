# Huawei Switch-local 路由开发计划

这份文档记录下一阶段的实现边界：把 `FatTreeSwitch` 已有的交换机本地 ECMP/RR 语义复制到 Huawei 拓扑里，同时保留 Huawei 自己的 L0/L1/OCS 层级、合口 OCS 图、SprayPoint 和 KSP 路由。

## Huawei 正式语义

正式 Huawei 拓扑必须和 `FatTreeTopology + FatTreeSwitch` 是同一种仿真思路：

```text
source rank 只携带到第一跳 L0 的 route
L0/L1 交换机收到包后，根据本地 FIB 和策略选择下一跳
每一跳只决定自己的 egress，不预先塞完整端到端 path
```

也就是说，目标一定是：

```text
rank -> L0
       L0 switch-local FIB
       L1 switch-local FIB
       OCS/SprayPoint/KSP switch-local next-hop
       L1 switch-local FIB
       L0 switch-local host route
```

不能把完整路径一次性写成：

```text
rank -> L0 -> L1 -> OCS/L1... -> L1 -> L0 -> rank
```

旧版 `main_uec.cpp` 里曾经有一段手工拼完整 `Route` 的临时 smoke 分支，用来快速验证 OCS 图、链路速率、同 tray 直连、KSP/SprayPoint path 生成。该分支不是最终 Huawei 数据面的语义，已经移除；正式 Huawei OCS 模式只走 switch-local FIB。

正式实现需要验证的行为是：

```text
L0/L1 每一跳根据本交换机 FIB 和策略选择下一跳
ecmp_rr 对数据包逐包 RR
ecmp_host / flow_hash 对同一 flow 固定下一跳
adaptive routing 后续能读取本地队列状态
```

## 从 FatTreeSwitch 复制什么

可以直接复用或近似复制的部分：

```text
receivePacket(Packet&)
  第一次进入 switch 时调用 getNextHop()
  pkt.set_route(next_hop)
  经过 CallbackPipe 模拟 switch latency
  第二次回调时从 egress queue sendOn()

RouteTable / FibEntry / HostFibEntry
  destination -> candidate egress Route list
  destination + flowid -> host delivery Route

ECMP/RR 选择逻辑
  ECMP: hash(flow_id, pathid, switch_salt)
  RR: 数据包逐包 round-robin，小控制包仍 hash
  ECMP_ADAPTIVE / ADAPTIVE_ROUTING 后续可按队列状态启用

permute_paths()
  RR 每轮打散候选路径顺序

addHostPort()
  目的 rank 接到本 switch 下方时，注册到对应 UEC sink/source port
```

## 不能复制什么

不能复制 `FatTreeSwitch::getNextHop()` 里的 fat-tree pod/tier 公式：

```text
HOST_POD_SWITCH()
HOST_POD()
MIN_POD_AGG_SWITCH()
MAX_POD_AGG_SWITCH()
CORE -> AGG 的 podpos 映射
```

这些公式只适用于传统 fat-tree。Huawei 需要自己的映射：

```text
rank -> group / L0 domain / tray / xpu
port -> plane
L0 -> same-plane L1 EPS candidate set
L1 -> same-group L0 downlink candidate set
L1 -> cross-group OCS next hop
```

## 建议新增文件

```text
htsim/sim/datacenter/huawei_switch.h
htsim/sim/datacenter/huawei_switch.cpp
htsim/sim/datacenter/huawei_topology.h
htsim/sim/datacenter/huawei_topology.cpp
```

当前约定是：只要启用了 `-huawei_ocs_mode spraypoint|ksp`，主程序就构建 `HuaweiTopology`，源端 `Route` 只到第一跳 L0；L0/L1 后续转发由 `HuaweiSwitch` 的本地 FIB 和 L1 OCS special resolver 决定。不再提供 legacy 完整路径开关。

## HuaweiSwitch 职责

`HuaweiSwitch` 负责本地转发，不负责生成全局路径。

建议类型：

```cpp
enum class HuaweiSwitchType {
    L0,
    L1,
};

enum class HuaweiSwitchStrategy {
    ECMP,
    RR,
    ADAPTIVE,
    ECMP_ADAPTIVE,
};
```

核心接口：

```cpp
void addRoute(int dst, Route* egress, packet_direction direction);
void addHostRoute(int dst, int flowid, Route* egress);
Route* getNextHop(Packet& pkt, BaseQueue* ingress_port) override;
void receivePacket(Packet& pkt) override;
```

`getNextHop()` 顺序：

```text
1. 如果有 host route，直接向下交给 UEC sink/source port。
2. 否则查普通 FIB：dst rank -> egress Route candidates。
3. 如果候选数 > 1，根据策略 ECMP/RR/AR 选择。
4. 没有 route 就 fail fast，打印 switch 名称、类型、dst、flow_id。
```

这样 L0/L1 的 ECMP/RR 行为就和 FatTreeSwitch 对齐。

## HuaweiTopology 职责

`HuaweiTopology` 负责建图和填 FIB。

### 链路对象

每条有向 link 仍是：

```text
Queue -> Pipe -> remote_endpoint
```

链路层级：

```text
local_tray       800G，服务器内部直连
huawei_host_l0   100G，rank 外部端口到 L0
huawei_l0_l1     100G，L0 到同 plane L1 EPS
huawei_l1_ocs    100G，L1 通过 OCS 到其他 L1
```

### rank 映射

8192 卡 `N=8,M=4` 口径：

```text
groups = 16
ranks_per_group = 512
ranks_per_tray = 8
l1_planes = M = 4
l1_eps_per_l1_plane = 4
logical_nodes_per_group = l1_planes * l1_eps_per_l1_plane / 2 = 8
```

第一版可以继续沿用当前显式分支的简化映射进行功能验证：

```text
tray = rank / ranks_per_tray
l0_id = tray
group = rank / ranks_per_group
plane = port
```

后续再把 L0 domain 的 8 tray / 8 L0 EPS 完整展开。

### FIB 填充

连接建立时不再写完整端到端路径，而是：

```text
uec_src port p:
  routeout = rank(src) -> L0(src, plane p)
  routeback = rank(dst) -> L0(dst, plane p)

topology:
  register destination host route on dst-side L0
  register reverse host route on src-side L0
  ensure L0/L1/OCS FIB entries exist for dst rank
```

对于一个外部 flow：

```text
Source rank
  -> source L0
  -> L0 switch-local: choose one L1 in same plane
  -> L1 switch-local:
       same group: choose downlink L0
       cross group: OCS SprayPoint/KSP next hop
  -> destination group L1
  -> L1 switch-local: choose dst L0
  -> dst L0 switch-local: host route to sink
```

## L0/L1 的 ECMP/RR 语义

L0:

```text
同一 dst rank 可以有多个同 plane L1 candidate。
ecmp_host / flow_hash: 同一 flow 固定一个 L1。
ecmp_rr: 数据包在这些 L1 candidate 间逐包轮询。
```

L1:

```text
same group 下行时，可以有多个 dst L0 candidate 或 bundle。
cross group 上行时，交给 OCS route policy。
```

这里的关键是：L0/L1 都不应该知道完整端到端 path，只知道“下一跳候选集合”。

## OCS 路由接入

OCS 只在 cross-group L1 上触发。

SprayPoint：

```text
current logical node + dst_group -> choose next logical node
source step 使用 source_spray_next_hops()
middle step 使用 pointing_next_hops()
到达 dst_group 任意 logical node 后退出 OCS
```

KSP：

```text
source L1:
  choose complete path_id
  pkt.set_ocs_ksp_route(src_node, dst_group, path_id)
middle L1:
  pkt.has_ocs_ksp_route()
  next = ksp.next_hop(src_node, dst_group, path_id, current_node)
destination group:
  pkt.clear_ocs_ksp_route()
  进入 group-local downlink
```

`Packet` 里已经有 `ocs_ksp_route` 元数据字段，可以直接使用。

## 验证矩阵

最小功能测试：

```text
1. same tray:
   0 -> 1，只走 local_tray 800G，不经过 L0/L1/OCS。

2. same group cross tray:
   0 -> 16，经过 host_l0 和 l0_l1，不出现 huawei_l1_ocs。

3. cross group SprayPoint:
   0 -> 512，出现 huawei_l1_ocs，终点进入 dst group。

4. cross group KSP:
   k=2/4/8/16，检查 packet metadata 和 next_hop 链条。

5. L0/L1 flow_hash:
   单 flow 的每个 switch 只选一个 candidate。

6. L0/L1 packet_rr:
   单 flow 数据包在 candidate 间分散。

7. 随机 256 rank in 8192:
   复用 EP256 traffic matrix，确认 60000+ flow 能完成，link_load 层级合理。
```

## 分阶段实现

第一阶段：

```text
1. 新增 HuaweiSwitch，复制 FatTreeSwitch 的 receivePacket/FIB/ECMP/RR。
2. 新增最小 HuaweiTopology，用当前简化 rank->tray->L0 映射。
3. `main_uec` 在 `-huawei_ocs_mode spraypoint|ksp` 时直接进入 Huawei switch-local topology。
4. 旧完整路径 smoke 分支已删除，避免与正式 Huawei 语义混用。
5. 补 smoke：单 flow 的 flow_hash / packet_rr 是否真的改变 L0/L1 active links。
```

第二阶段：

```text
1. 把 L0 domain 完整展开为 8 tray / 8 L0 EPS。
2. 支持每个 L0 EPS 下行 8、上行 4 的 1:2 收敛比。
3. 把 256-rank EP traffic matrix 放到 8192 rank 集群中随机采样测试。
```

第三阶段：

```text
1. 加 adaptive routing。
2. 对接 GOAL/ATLAHS。
3. 增加大规模链路负载图和 OCS path dump。
```
