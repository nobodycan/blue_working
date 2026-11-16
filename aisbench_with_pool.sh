#!/usr/bin/env bash
# 用法：在工具目录执行 bash /路径/aisbench_with_pool.sh 前缀文件.jsonl 完整数据文件.jsonl
# 使用无池10前缀测试生成的同一份数据。config.py设为default_perf。
# 服务端须已开启Mooncake KV池和本地prefix cache；本脚本不启动/开关池。
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "用法：bash aisbench_with_pool.sh 前缀文件.jsonl 完整数据文件.jsonl" >&2
  exit 1
fi
PREFIX_FILE=$1
FULL_FILE=$2
[[ -f aisbench_test.py ]] || { echo "请先cd到aisbench_auto_tools_prefix目录" >&2; exit 1; }
[[ -f "$PREFIX_FILE" && -f "$FULL_FILE" ]] || { echo "数据文件不存在" >&2; exit 1; }
[[ $(awk 'NF {n++} END {print n+0}' "$PREFIX_FILE") -eq 10 ]] || { echo "前缀文件必须是10条" >&2; exit 1; }
[[ $(awk 'NF {n++} END {print n+0}' "$FULL_FILE") -eq 30 ]] || { echo "完整数据文件必须是30条" >&2; exit 1; }

echo "先预热10条前缀，并发1。"
python3 aisbench_test.py \
  --dataset "$PREFIX_FILE" --dataset_type normal \
  --output_len 1 --concurrency 1 --data_num 10 \
  --request_rate 0 --test_type stream \
  --repeat 1 --npu_num 8

read -r -p "请确认本轮前缀写池完成、无错误；输入yes开始正式测试：" answer
[[ "$answer" == yes ]] || { echo "未确认，停止测试。" >&2; exit 1; }

echo "有池正式测试：10条前缀，90%，128K输入/1输出，10并发，30条。"
python3 aisbench_test.py \
  --dataset "$FULL_FILE" --dataset_type prefix_cache \
  --input_len 131072 --output_len 1 \
  --repeat_rate 0.9 --prefix_num 10 --dp 1 \
  --concurrency 10 --data_num 30 \
  --request_rate 0 --test_type stream \
  --repeat 1 --npu_num 8

# 装订版本，work by claude code，2026-11-16
