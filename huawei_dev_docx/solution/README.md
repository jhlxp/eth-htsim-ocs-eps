# MoE TE 理论建模

这里放置 EP256 all-to-all 在 Huawei 8192-rank OCS/EPS 拓扑上的离线流量放置模型。

当前保留两个算法结果目录：

```text
lpt_pod_local_results/   # pod-local LPT，发送侧和接收侧分别按 pod 做负载感知放置
ecmp_results/            # ECMP/hash baseline，单 flow hash，不做负载感知
```

推荐对比脚本：

```bash
python3 /home/chen/workplace/infra/HTSIM/huawei_dev_docx/solution/lpt_pod_local_model.py
python3 /home/chen/workplace/infra/HTSIM/huawei_dev_docx/solution/ecmp_te_model.py
```

默认参数：

- EP ranks：256
- topology：8192 ranks，16 groups/pods，512 ranks/group，8 ranks/tray
- 外部 plane / NIC 数：4
- 每个 group、每个 plane 的 L1 EPS：4
- traffic：单层 dispatch-only，`tokens_per_rank=4096`，`topk=8`
- placement：256 个 active rank 随机落到指定 pod 数内，不强制均摊
- pod 数：16、8、4、2

算法口径：

- LPT：flow 按大小降序；发送侧按源 pod 独立选择源 L0/L1 EPS；接收侧按目的 pod 独立选择目的 L0/L1 EPS。
- ECMP：每条 flow 用 deterministic hash 选择源 L0/L1 EPS 和目的 L0/L1 EPS，不使用流量大小和当前负载。

早期串行 LPT 脚本 `lpt_te_model.py` 和结果 `lpt_results/` 仍保留，主要用于回溯；当前建议看 `lpt_pod_local_results/`。

结果默认写到各自目录：

```text
/home/chen/workplace/infra/HTSIM/huawei_dev_docx/solution/lpt_pod_local_results/
/home/chen/workplace/infra/HTSIM/huawei_dev_docx/solution/ecmp_results/
```
