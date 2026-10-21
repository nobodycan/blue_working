#!/usr/bin/env bash
set -euo pipefail
source /bench/env.sh
p_hosts=(); p_ports=(); d_hosts=(); d_ports=()
for host in "${P_IPS[@]}"; do
  for port in {7100..7107}; do p_hosts+=("$host"); p_ports+=("$port"); done
done
for host in "${D_IPS[@]}"; do
  for port in {7100..7107}; do d_hosts+=("$host"); d_ports+=("$port"); done
done
proxy=/bench/upstream/load_balance_proxy_server_example.py
if [[ "${PROXY_AUDIT:-0}" == 1 ]]; then
  python /bench/scripts/patch_proxy.py "$proxy" /bench/upstream/proxy_instrumented.py
  proxy=/bench/upstream/proxy_instrumented.py
  export PROXY_AUDIT_DIR=/bench/results/proxy-audit
fi
exec python "$proxy" --host "$PROXY_HOST" --port "$PROXY_PORT" \
  --workers 1 --max-retries 1 \
  --prefiller-hosts "${p_hosts[@]}" --prefiller-ports "${p_ports[@]}" \
  --decoder-hosts "${d_hosts[@]}" --decoder-ports "${d_ports[@]}"
