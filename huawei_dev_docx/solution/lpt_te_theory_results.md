# LPT TE 理论结果说明

## 模型

- 输入流量：EP256 单层 dispatch-only all-to-all。
- Flow 数：65172。
- 总流量：111.562 GiB。
- Placement：256 个 active rank 随机落入指定数量的 pod/group，不强制均摊。
- 路由约束：每个 flow 是 single path，不做 packet spray。
- LPT 决策：先在源 tray 的 4 个 L0 EPS 中选最小负载，再在源 group/plane 的 4 个 L1 EPS 中选最小负载。

## 结果表

| active pods | L0 全局 max/mean | L0 tray 内 max/mean | L1 全局 max/mean | L1 pod 内 max/mean | L1 group-plane 内 max/mean | L1 max GiB |
|---:|---:|---:|---:|---:|---:|---:|
| 16 | 2.8415 | 1.000114 | 1.4386 | 1.000069 | 1.000018 | 0.627 |
| 8 | 2.6192 | 1.000104 | 1.2557 | 1.000052 | 1.000008 | 1.094 |
| 4 | 2.5825 | 1.000085 | 1.1161 | 1.000036 | 1.000003 | 1.946 |
| 2 | 2.3936 | 1.000053 | 1.0490 | 1.000023 | 1.000001 | 3.657 |

## LPT 算法开销

这里只统计 C++ `lpt_assign_core()` 的算法开销，不包含读 CSV、生成流量、Python 对象记录、写文件和画图。`sort` 是 flow 按大小分桶并按 bucket 降序排列，`greedy` 是逐 flow 选择 L0/L1 EPS 并累加负载。

| active pods | trials | flow count | sort ms | greedy ms | total ms | total us/flow |
|---:|---:|---:|---:|---:|---:|---:|
| 2 | 16 | 65172 | 0.6289 | 1.1275 | 1.7564 | 0.0269 |
| 4 | 16 | 65172 | 0.6235 | 1.1527 | 1.7762 | 0.0273 |
| 8 | 16 | 65172 | 0.6303 | 1.1473 | 1.7776 | 0.0273 |
| 16 | 16 | 65172 | 0.6872 | 1.2662 | 1.9534 | 0.0300 |

## 结论

- **L1 在每个 pod 内已经均衡**：`L1 pod 内 max/mean` 约为 `1.00002-1.00007`，基本就是完全打平。
- **L1 在每个 group-plane 内更均衡**：4 个 L1 EPS 的 `max/mean` 约为 `1.000001-1.000018`。这说明 LPT 在第二级候选 EPS 上工作正常。
- **L1 全局 max/mean 不是算法不均衡**：它主要来自随机 placement 后不同 pod 的源流量总量不同。例如 detail trial 里 pods=16 时，各 pod active rank 数从 8 到 22 不等，pod egress 自然不同。
- **L0 全局不均衡也正常**：L0 被源 tray 总流量约束，热点和随机 placement 会让不同 tray 的总 egress 不一样；但 `L0 tray 内 max/mean` 约为 `1.00005-1.00011`，说明每个 tray 内的 4 个 L0 plane 也被打平。

## 关键文件

- `lpt_te_model.py`：理论建模脚本。
- `lpt_results/lpt_summary_aggregate.csv`：聚合结果。
- `lpt_results/lpt_summary_by_trial.csv`：每个随机 trial 的结果。
- `lpt_results/traffic_by_pod_detail_trial.csv`：detail trial 的 pod active rank、收发总量。
- `lpt_results/loads_pods*_trial0_l1.csv`：detail trial 的 L1 EPS 负载。
- `lpt_results/assignments_pods*_trial0.csv`：flow 到 L0/L1 的 LPT 决策明细。

## L1 EPS 负载图

- `lpt_results/l1_eps_load_by_pod_pods2_trial0.png`：2pod 场景，每个 pod 内 16 个 L1 EPS 的实际承载量。
- `lpt_results/l1_eps_load_by_pod_pods4_trial0.png`：4pod 场景，每个 pod 内 16 个 L1 EPS 的实际承载量。
- `lpt_results/l1_eps_load_by_pod_pods8_trial0.png`：8pod 场景，每个 pod 内 16 个 L1 EPS 的实际承载量。
- `lpt_results/l1_eps_load_by_pod_pods16_trial0.png`：16pod 场景，每个 pod 内 16 个 L1 EPS 的实际承载量。

## L0 EPS 负载图

- `lpt_results/l0_eps_load_by_pod_pods2_trial0.png`：2pod 场景，每个 pod 内 active tray 的 L0 EPS 实际承载量。
- `lpt_results/l0_eps_load_by_pod_pods4_trial0.png`：4pod 场景，每个 pod 内 active tray 的 L0 EPS 实际承载量。
- `lpt_results/l0_eps_load_by_pod_pods8_trial0.png`：8pod 场景，每个 pod 内 active tray 的 L0 EPS 实际承载量。
- `lpt_results/l0_eps_load_by_pod_pods16_trial0.png`：16pod 场景，每个 pod 内 active tray 的 L0 EPS 实际承载量。
