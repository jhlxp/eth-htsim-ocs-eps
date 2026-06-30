#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export STRAT="${STRAT:-ecmp_host}"
export HUAWEI_OCS_MODE="${HUAWEI_OCS_MODE:-spraypoint}"
export HUAWEI_OCS_CHOICE="${HUAWEI_OCS_CHOICE:-flow_hash}"
export ROUTE_PLAN_ALGO="${ROUTE_PLAN_ALGO:-lpt}"
export HUAWEI_SOURCE_PORTS="${HUAWEI_SOURCE_PORTS:-1}"
export LOG="${LOG:-htsim_singleflow_lpt_source_route_${HUAWEI_OCS_MODE}.log}"

exec "${SCRIPT_DIR}/run_huawei_ep256_spray.sh"
