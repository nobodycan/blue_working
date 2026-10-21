#!/usr/bin/env bash
set -euo pipefail
source /bench/env.sh
mkdir -p /bench/results/environment /bench/logs /bench/profiles /bench/upstream
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="/bench/results/environment/$(hostname)-$stamp"
mkdir -p "$out"
npu-smi info | tee "$out/npu-smi.txt"
cat /usr/local/Ascend/driver/version.info > "$out/driver-version.txt"
python -m pip freeze > "$out/pip-freeze.txt"
vllm serve --help=all > "$out/serve-help.txt"
vllm bench serve --help > "$out/bench-help.txt"
python - <<'PY' | tee "$out/python-stack.txt"
import importlib.metadata as m
import json, os
import torch, torch_npu, vllm, vllm_ascend
from packaging.version import Version
for name in ('vllm', 'vllm-ascend', 'torch', 'torch-npu', 'transformers', 'triton-ascend'):
    print(name, m.version(name))
assert Version(m.version('vllm')).base_version == '0.23.0', 'Wrong vLLM version'
assert Version(m.version('vllm-ascend')).base_version == '0.23.0', 'Wrong Ascend version'
assert torch.npu.is_available()
assert torch.npu.device_count() == 8, 'This recipe assumes eight visible A2 devices'
config = json.load(open(os.path.join(os.environ['MODEL'], 'config.json')))
print('model_type:', config.get('model_type'))
assert config.get('model_type') == 'deepseek_v4'
print('quantization_config:', config.get('quantization_config'))
print('vllm_ascend:', vllm_ascend.__file__)
print('torch_npu:', torch_npu.__file__)
print('PREFLIGHT PYTHON CHECK PASSED')
PY
find "$MODEL" -maxdepth 1 -type f \
  \( -name '*.json' -o -name '*.py' -o -name '*.model' \) \
  -exec sha256sum {} + > "$out/model-metadata-sha256.txt"
for flag in --no-enable-prefix-caching --kv-cache-memory-bytes --profiler-config \
            --no-disable-hybrid-kv-cache-manager --speculative-config; do
  grep -q -- "$flag" "$out/serve-help.txt" || { echo "Missing required flag: $flag"; exit 1; }
done
printf 'Environment evidence: %s\n' "$out"
