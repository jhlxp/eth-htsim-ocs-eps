# HTSIM Tests Layout

This directory keeps only runnable test and experiment entrypoints.

## Directories

- `functional/`: small correctness and smoke-test bash scripts.
- `experiments/`: experiment entrypoints and experiment-specific data generators.
- `plot/`: plotting and result post-processing scripts.
- `log/`: generated run directories. This directory is intentionally disposable.

## Current Commands

Huawei functional checks:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/functional/run_huawei_switch_local_smoke.sh
./HTSIM/tests/functional/run_huawei_ocs_dataplane_tests.sh
./HTSIM/tests/functional/run_ocs_m4n8_feature_tests.sh
```

Current DeepSeek-R1 EP256 empirical experiment:

```bash
cd /home/chen/workplace/infra
./HTSIM/tests/experiments/run_deepseek_r1_empirical_full.sh
```

