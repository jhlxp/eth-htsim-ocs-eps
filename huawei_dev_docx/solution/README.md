# LPT TE 理论建模

这里放置 EP256 all-to-all 在 Huawei 8192-rank OCS/EPS 拓扑上的离线 LPT 负载均衡模型。

核心脚本：

```bash
python3 /home/chen/workplace/infra/HTSIM/huawei_dev_docx/solution/lpt_te_model.py
```

默认参数：

- EP ranks：256
- topology：8192 ranks，16 groups/pods，512 ranks/group，8 ranks/tray
- 外部 plane / NIC 数：4
- 每个 group、每个 plane 的 L1 EPS：4
- traffic：单层 dispatch-only，`tokens_per_rank=4096`，`topk=8`
- placement：256 个 active rank 随机落到指定 pod 数内，不强制均摊
- pod 数：16、8、4、2
- LPT：flow 按大小降序，先选源 tray 的 L0 EPS，再选源 group/plane 的 L1 EPS

结果默认写到：

```text
/home/chen/workplace/infra/HTSIM/huawei_dev_docx/solution/lpt_results/
```
