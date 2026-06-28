# L2 OCS KSP 路由

`KSP` 是 Huawei L2 OCS 跨 group 流量的完整路径路由模式。它运行在 `03_l2_ocs_graph.md` 描述的 coupled logical OCS graph 上。

实现文件：

```text
htsim/sim/datacenter/huawei_ocs_ksp.h
htsim/sim/datacenter/huawei_ocs_ksp.cpp
htsim/sim/datacenter/huawei_ocs_ksp_dump.cpp
```

## 路由范围

当 L1 EPS 看到跨 group 流量时，才使用 KSP：

```text
current_group != dst_group
```

路径目标是目的 group：

```text
dst_set = all logical_node(dst_group, *, *)
```

路径到达 `dst_group` 中任意 logical node 后，OCS segment 结束。之后由 group-local L1/L0 FIB 送到目的 rank。

## 参数

```text
-huawei_ocs_mode ksp
-huawei_ocs_choice packet_rr|flow_hash
-huawei_ksp_k <K>
-huawei_ksp_max_hops auto|<hops>
-huawei_ksp_seed <seed>
-huawei_ksp_max_paths_per_pair <limit>
```

含义：

- `huawei_ksp_k`: 每个 `(src_logical_node, dst_group)` 保留的候选 path 数量。
- `huawei_ksp_max_hops`: 最大 OCS hop 数；`auto` 表示不设置较小的显式上限。
- `huawei_ksp_seed`: 确定性 tie-break seed。
- `huawei_ksp_max_paths_per_pair`: path generation 的保护上限。
- `huawei_ocs_choice`: 运行时 path 选择策略。

默认实验值：

```text
ksp_k = 8
ksp_max_hops = auto
ksp_seed = 42
huawei_ocs_choice = packet_rr
```

## Path Table

router 会预计算：

```text
paths[src_logical_node][dst_group] = [
  [src_logical_node, ..., dst_group_logical_node],
  ...
]
```

每条 path 满足：

- 从 source logical node 开始；
- 在 `dst_group` 的任意 logical node 结束；
- 是 simple path；
- 按 hop count 排序，并使用确定性 tie-break。

实现类：

```cpp
class HuaweiOcsKspRouter
```

主要 API：

```cpp
const vector<HuaweiOcsKspPath>& paths(uint32_t src_node, uint32_t dst_group) const;
uint32_t choose_path(...);
uint32_t next_hop(src_node, dst_group, path_id, current_node) const;
```

## Path 生成

实现使用 Yen-style K shortest simple path search，目标是一个 destination group node set。

流程：

```text
1. 用 BFS 找 src_node 到 dst_group 任意 node 的第一条 shortest path。
2. 加入 accepted paths。
3. 对已接受 path 的各个 spur 位置生成偏离候选。
4. 禁止重建已接受 path 的相同 prefix edge。
5. 禁止 root-prefix node，保证 path simple。
6. 按长度和确定性 tie-break 选择最优候选。
7. 达到 K 条或无候选后停止。
```

这种方式避免在 expander graph 上枚举所有可能 path。

## Packet Metadata

KSP 需要 packet metadata，因为源侧选择的是完整 OCS path，中间 L1 EPS 必须沿着这条 path 继续转发。

packet 保存：

```cpp
bool has_ocs_ksp_route() const;
void set_ocs_ksp_route(uint32_t src_node, uint32_t dst_group, uint32_t path_id);
void clear_ocs_ksp_route();

uint32_t ocs_ksp_src_node() const;
uint32_t ocs_ksp_dst_group() const;
uint32_t ocs_ksp_path_id() const;
```

packet 重新初始化时会清除 metadata；packet 到达目的 group 时也会清除 metadata。

## 运行时转发

源 L1 EPS：

```text
src_node = current logical node
dst_group = rank_group(pkt.dst)
path_id = choose_path(src_node, dst_group, pkt)
pkt.set_ocs_ksp_route(src_node, dst_group, path_id)
next = next_hop(src_node, dst_group, path_id, current_node)
```

中间 L1 EPS：

```text
src_node = pkt.ocs_ksp_src_node()
dst_group = pkt.ocs_ksp_dst_group()
path_id = pkt.ocs_ksp_path_id()
current_node = current logical node
next = next_hop(src_node, dst_group, path_id, current_node)
```

目的 group：

```text
pkt.clear_ocs_ksp_route()
return nullptr from OCS resolver
HuaweiSwitch continues with normal downlink FIB
```

## Path 选择

`-huawei_ocs_choice` 控制从 K 条候选 path 中选哪一条：

```text
packet_rr:
  source L1 switch-local round-robin over K paths

flow_hash:
  stable hash over flow_id/pathid/src/dst_group
```

`packet_rr` 用于压测 packet-level path spreading。`flow_hash` 更接近传统 ECMP/KSP 的 flow placement。

## Data-Plane 集成

集成入口：

```cpp
HuaweiTopology::l1_special_next_hop(HuaweiSwitch* sw, Packet& pkt)
```

KSP 调用：

```cpp
if (!pkt.has_ocs_ksp_route()) {
    path_id = _ksp->choose_path(...);
    pkt.set_ocs_ksp_route(src_node, dst_group, path_id);
}

next_node = _ksp->next_hop(src_node, dst_group, path_id, current_node);
```

logical next node 会映射回物理 L1 EPS，并保持当前 coupled member：

```cpp
next_l1 = l1_from_logical_node(next_node, current_member)
```

## 检查 KSP Paths

编译：

```bash
cd /home/chen/workplace/infra/HTSIM
cmake --build htsim/sim/build --target huawei_ocs_ksp_dump -j 8
```

查看 N=8,M=4、K=8：

```bash
./htsim/sim/datacenter/huawei_ocs_ksp_dump \
  --coupled \
  --groups 16 \
  --l1-planes 4 \
  --l1-eps-per-l1-plane 4 \
  --degree 64 \
  --ocs_expander_seed 42 \
  --k 8 \
  --max-hops auto \
  --ksp-seed 42
```

运行功能测试：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

运行 EP256 KSP 实验：

```bash
cd /home/chen/workplace/infra
HUAWEI_OCS_MODE=ksp HUAWEI_OCS_CHOICE=packet_rr KSP_K=8 \
  ./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```
