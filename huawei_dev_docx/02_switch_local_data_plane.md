# Switch-Local Huawei Data Plane

这份文档说明 Huawei 拓扑当前实现的数据面逻辑。

核心规则：

```text
源端 route 只到第一跳 L0；
后续每一跳由 HuaweiSwitch 本地 FIB 或 L1 OCS resolver 决定。
```

Huawei 拓扑不会生成完整的端到端 path list。

## 数据面对象

拓扑对象：

```cpp
HuaweiTopology
```

交换机对象：

```cpp
HuaweiSwitch
```

OCS 特殊下一跳 resolver：

```cpp
HuaweiTopology::l1_special_next_hop(...)
```

UEC 建连入口：

```cpp
HuaweiTopology::connect_endpoints(src, dst, uec_src, uec_snk, start_time)
```

## Route Setup

同 tray 流量：

```text
rank_src -> local direct 800G link -> rank_dst
```

本地直连 route 会挂到所有 UEC port 上，但不会进入 L0/L1/OCS。

外部网络流量中，每个 UEC port 映射到一个 plane：

```text
port p:
  source route = rank_src -> L0(src,p)
  reverse route = rank_dst -> L0(dst,p)
```

第一段 route fragment 到 L0 结束。之后由 switch-local forwarding 接管。

## L0 FIB

对每个 active destination，源侧 L0 switch 安装上行 FIB：

```text
L0(src, plane p) -> src_group 内同 plane 的所有 L1 EPS
```

候选数：

```text
l1_eps_per_l1_plane
```

当前实验中：

```text
l1_eps_per_l1_plane = 4
```

使用 `-strat ecmp_rr` 时，data packet 会在这些 L1 EPS 候选之间逐包 RR。

## L1 组内 FIB

对每个 destination rank，目的 group 的 L1 switch 安装下行 FIB：

```text
L1(dst_group, plane p, eps e) -> L0(dst,p)
```

如果 packet 到达任意目的 group 内的 L1 switch，OCS resolver 返回 `nullptr`，随后普通 FIB lookup 会把 packet 下送到目的 L0 和 rank。

## L1 跨组 OCS Resolver

当 L1 switch 看到：

```text
current_group != dst_group
```

它会调用：

```cpp
HuaweiTopology::l1_special_next_hop(...)
```

resolver 先把物理 L1 EPS 映射到 coupled logical node：

```text
current_node = logical_node(group, l1_plane, coupled_pair)
```

再根据配置选择 OCS 下一跳：

```text
spraypoint
ksp
```

选出的 logical node 会映射回物理 L1 EPS，并保持当前 coupled member：

```text
next_l1 = l1_from_logical_node(next_node, current_member)
```

物理链路为：

```text
L1(current) -> L1(next)
```

link-load sampler 中记录为：

```text
huawei_l1_ocs
```

## HuaweiSwitch 策略

`HuaweiSwitch` 复用了 fat-tree switch 中对本地下一跳选择有用的语义，但不依赖 fat-tree 的 pod/tier 公式。

支持：

```text
ECMP
RR
```

CLI 映射：

```text
-strat ecmp_host -> HuaweiSwitch::ECMP
-strat ecmp_rr   -> HuaweiSwitch::RR
```

选择规则：

```text
ECMP:
  hash(flow_id, pathid, switch_salt)

RR:
  if packet size < 128 bytes:
      hash(flow_id, pathid, switch_salt)
  else:
      switch-local packet round-robin
```

OCS `packet_rr` 使用独立的 `HuaweiSwitch::next_special_rr()` counter，因此 OCS packet spray 不依赖 UEC path entropy。

## Packet Metadata

Huawei OCS 在 `Packet` 中增加两类状态。

SprayPoint：

```cpp
bool ocs_source_sprayed() const;
void mark_ocs_source_sprayed();
void clear_ocs_source_sprayed();
```

它用于保证 source spray 只发生一次。

KSP：

```cpp
bool has_ocs_ksp_route() const;
void set_ocs_ksp_route(src_node, dst_group, path_id);
void clear_ocs_ksp_route();
```

它让中间 L1 EPS 能继续沿着源侧选择的完整 KSP path 转发。

两类 metadata 都会在 packet 重新初始化时清除。

## Link 名称与层级

拓扑会创建带名称的 queue/pipe link。link-load sampler 根据名称解析层级：

```text
LOCAL_SRCx->DSTy        -> huawei_local_direct
SRCx->L0_i              -> huawei_host_l0 up
L0_i->DSTx              -> huawei_host_l0 down
L0_i->L1_j              -> huawei_l0_l1 up
L1_j->L0_i              -> huawei_l0_l1 down
L1_i->L1_j              -> huawei_l1_ocs cross
```

采样输出：

```text
output_metrics/link_info.csv
output_metrics/link_load_1ms.csv
output_metrics/link_load_summary.csv
output_metrics/link_load_by_layer.png
```

## Runtime Logging

Huawei mode 会打印结构化配置块：

```text
#----------- HUAWEI TOPOLOGY begin ------------
#----------- HUAWEI TOPOLOGY END ------------

#----------- HUAWEI ROUTING begin ------------
#----------- HUAWEI ROUTING END ------------

#----------- HUAWEI LINK_QUEUE begin ------------
#----------- HUAWEI LINK_QUEUE END ------------

#----------- HUAWEI OUTPUT begin ------------
#----------- HUAWEI OUTPUT END ------------
```

这些块包含输入参数和派生统计，例如：

```text
groups
ranks_per_group
ranks_per_tray
l1_planes_ports
l1_eps_per_l1_plane
ocs_degree
l0_switches
l1_switches
ocs_coupled_logical_nodes
ocs_full_cross_group_degree
ocs_cross_group_complete
```

## 功能验证

基础 smoke test：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_huawei_switch_local_smoke.sh
```

验证内容：

- 同 tray 800G direct route；
- 同 group L0/L1 switch-local 转发；
- 跨 group KSP packet RR；
- 跨 group KSP flow hash；
- 跨 group SprayPoint packet RR。

OCS dataplane test：

```bash
./HTSIM/tests/functional/run_huawei_ocs_dataplane_tests.sh
```

M=4,N=8 feature test：

```bash
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

验证内容：

- OCS graph degree 和无组内边；
- SprayPoint 状态和 candidate 行为；
- KSP 的 K=2/4/8/16 path；
- local direct、same-group、cross-group、packet-RR、flow-hash case；
- 8192-rank 拓扑中嵌入 256-rank random all-to-all sample。

## 结果阅读

EP256 empirical 实验中最常看的文件：

```text
run_config.txt
htsim_<mode>_<choice>_<strategy>.log
output_metrics/flowsInfo.csv
output_metrics/link_load_summary.csv
output_metrics/link_load_by_layer.png
```

典型判断方式：

- `huawei_host_l0 up` 和 `huawei_l0_l1 up` 越均匀，说明 source spray 和 L0 RR 越正常。
- `huawei_l1_ocs cross` 反映跨 group OCS 的负载均衡情况。
- `huawei_host_l0 down` 和 `huawei_l0_l1 down` 可能仍然倾斜，因为 expert hotness 会导致 receiver-side incast。
