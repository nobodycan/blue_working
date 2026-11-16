#!/usr/bin/env bash
# 用法：在aisbench_auto_tools_prefix目录执行 bash /路径/aisbench_no_pool.sh 1或10
# config.py先填好服务地址/模型路径，DEFAULT_PERFORMANCE_TEST设为default_perf。
# 服务端须关闭KV池、开启本地prefix cache；本脚本不修改服务配置。
set -euo pipefail

PREFIX_NUM=${1:-1}
if [[ $# -gt 1 || ( "$PREFIX_NUM" != 1 && "$PREFIX_NUM" != 10 ) ]]; then
  echo "用法：bash aisbench_no_pool.sh 1或10" >&2
  exit 1
fi
[[ -f aisbench_test.py ]] || { echo "请先cd到aisbench_auto_tools_prefix目录" >&2; exit 1; }

echo "无池测试：${PREFIX_NUM}条前缀，90%，128K输入/1输出，10并发，30条。"
echo "自动生成并预热。请保存日志里的前缀文件和完整数据文件路径。"
python3 aisbench_test.py \
  --dataset_type prefix_cache --prefix_test \
  --input_len 131072 --output_len 1 \
  --repeat_rate 0.9 --prefix_num "$PREFIX_NUM" --dp 1 \
  --concurrency 10 --data_num 30 \
  --request_rate 0 --test_type stream \
  --repeat 1 --npu_num 8

# 装订版本，work by claude code，2026-11-16
