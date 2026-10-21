#!/usr/bin/env bash
set -euo pipefail
source /bench/scripts/common.sh
proxy=/bench/upstream/load_balance_proxy_server_example.py
if [[ "${PROXY_AUDIT:-0}" == 1 ]]; then
  python /bench/scripts/patch_proxy.py "$proxy" /bench/upstream/proxy_instrumented.py
  proxy=/bench/upstream/proxy_instrumented.py
  export PROXY_AUDIT_DIR=/bench/results/proxy-audit
fi
exec python "$proxy" --host 0.0.0.0 --port "$PROXY_PORT" --workers 1 --max-retries 1 \
 --prefiller-hosts "$P_IP" --prefiller-ports 8100 \
 --decoder-hosts "$D_IP" --decoder-ports 8200
