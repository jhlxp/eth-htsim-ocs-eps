# HTSIM Tests and Experiments

这个目录只保留可运行入口和小工具，生成结果统一落到 `tests/log/`。

## 目录

- `functional/`：功能 smoke test，用来确认 Huawei switch-local FIB、OCS graph、SprayPoint/KSP 路由能跑通。
- `experiments/`：实验入口脚本和实验专用生成器。
- `plot/`：结果画图和后处理脚本。
- `data/`：EP256 empirical traffic 的输入分布。
- `log/`：每次运行生成一个独立 `run_*` 目录。

## 当前核心实验

三个脚本跑的是同一个 EP256 empirical workload，区别只在网络机制：

- `run_huawei_ep256_spray.sh`：packet spray baseline。
- `run_huawei_ep256_ecmp.sh`：single-flow ECMP。
- `run_huawei_ep256_lpt.sh`：single-flow + LPT source route。

### 1. Packet spray baseline

入口：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/experiments/run_huawei_ep256_spray.sh
```

默认语义：

- EP256 empirical single collective dispatch。
- Huawei 8192-rank topology，默认 `M=4,N=8` 参数族。
- 源端 `source_ports = l1_planes = 4`，因此允许 NIC spray。
- OCS 默认由脚本变量控制，当前默认是 `ksp + packet_rr`。

### 2. Single-flow + ECMP

入口：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/experiments/run_huawei_ep256_ecmp.sh
```

语义：

- `STRAT=ecmp_host`，交换机使用 flow-level ECMP。
- `HUAWEI_SOURCE_PORTS=1`，一个 UEC flow 只绑定一个源端口，不再做 NIC spray。
- `ROUTE_PLAN_ALGO=ecmp`，每条 flow 用 deterministic hash 选择一个源 L0 plane；后续仍由 L0/L1 switch-local ECMP 和 OCS SprayPoint 决策。
- OCS 默认 `spraypoint + flow_hash`。

### 3. Single-flow + LPT source route

入口：

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/experiments/run_huawei_ep256_lpt.sh
```

语义：

- `STRAT=ecmp_host`，单 flow 不做 packet spray。
- `HUAWEI_SOURCE_PORTS=1`。
- `ROUTE_PLAN_ALGO=lpt`，生成每条 flow 的 `src_l0_plane/src_l1_id/dst_l1_id/dst_l0_plane`。
- 仿真器按 route plan 安装 per-flow FIB，数据包按源路由计划走；没有计划的控制/反向流仍走默认 switch-local 路由。
- OCS 默认 `spraypoint + flow_hash`，但 route plan 中的 forward flow 会覆盖跨组 L1 下一跳。

## 常用变量

这些变量可以直接加在命令前覆盖：

```bash
QUEUE_PKTS=200 ENABLE_ECN=1 ECN_LOW_PKTS=120 ECN_HIGH_PKTS=160 PLOT=1 LINK_LOAD_SAMPLE=1 \
./HTSIM/tests/experiments/run_huawei_ep256_lpt.sh
```

- `EP_RANKS`：EP rank 数，默认 256。
- `RANK_PLACEMENT`：EP rank 嵌入方式，默认 `strided`。
- `TOKENS_PER_RANK` / `TOPK`：all-to-allv 规模，默认 `4096 / 8`。
- `ROUTE_PLAN_ALGO`：`none`、`ecmp`、`lpt`。
- `HUAWEI_SOURCE_PORTS`：UEC 源端口数。single-flow 实验设为 1，packet spray 设为 `L1_PLANES`。
- `HUAWEI_OCS_MODE`：`spraypoint` 或 `ksp`。
- `HUAWEI_OCS_CHOICE`：`flow_hash` 或 `packet_rr`。
- `QUEUE_PKTS`：交换机队列包数，默认 200。
- `ENABLE_ECN` / `ECN_LOW_PKTS` / `ECN_HIGH_PKTS`：Huawei ECNQueue 配置，默认开启，阈值为 120/160 packets。
- `LINK_LOAD_SAMPLE`：是否输出 `output_metrics/link_load_1ms.csv`。
- `ACTUAL_LOAD_PLOT`：是否基于真实链路采样画 EPS/OCS 负载图，默认开启。

## 输出

每次运行会生成：

- `run_config.txt`：实验参数快照。
- `data/deepseek_r1_ep256_empirical_aggregate.cm`：HTSIM traffic matrix。
- `data/huawei_route_plan_*.csv`：可选 per-flow route plan。
- `output_metrics/flowsInfo.csv`：flow completion result。
- `output_metrics/link_info.csv` 和 `output_metrics/link_load_1ms.csv`：链路信息和 1ms 采样负载。
- `output_metrics/<algo>_actual_load/`：基于真实链路采样的实际负载图。`src_l1_send` 只统计 `L0->L1` 上行进入 L1-EPS 的流量，`dst_l1_recv` 只统计 `L1->L0` 下行离开 L1-EPS 的流量；OCS 内部 `L1->L1` hop 单独放在 OCS 矩阵和 `ocs_l1_egress/ocs_l1_ingress` 统计里。
- `output_metrics/lpt_theory_load/`：仅 LPT 实验自动生成，是 route plan payload bytes 的离线理论分配图，不包含重传、RTS/ACK、ECN 标记或真实链路采样字节。
- `plots/` 和 `output_metrics/*.png`：输入分布和结果图。
