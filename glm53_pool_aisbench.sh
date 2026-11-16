#!/usr/bin/env bash
# GLM-5.3: AISBench + aisbench_auto_tools_prefix, Linux Bash >= 4.
# 装订版本，work by claude code，2026-11-16
set -Eeuo pipefail
umask 077

usage() {
  cat <<'HELP'
用法：
  bash glm53_pool_aisbench.sh prepare --tools-dir DIR --tokenizer DIR --data-dir NEW_DIR
  bash glm53_pool_aisbench.sh check --tools-dir DIR --tokenizer DIR [--ais-root DIR]
  bash glm53_pool_aisbench.sh run --tools-dir DIR --tokenizer DIR --data-dir DIR \
    --platform A2|H20 --scene 1|2|3 --host IP --port PORT --model NAME \
    --server-config FILE --cache-ready [--out-root DIR] [--ais-root DIR]

固定负载：131072输入、1输出、90%前缀、10并发、30条；单独预热，并发1。
场景1：1前缀、无池；场景2：10前缀、无池；场景3：10前缀、有池。
脚本不启动/重启模型、不清缓存、不安装依赖、不下载代码。
当前版本用于无鉴权的内部推理/metrics端点；不会读取或保存API密钥。
--server-config FILE  归档服务端实际启动命令/日志；不自动判断配置是否生效。
--cache-ready         操作者确认配置符合场景，且已隔离旧缓存、引擎空闲。
--metrics-url URL     默认 http://HOST:PORT/metrics；支持独立metrics地址。
--pool-check-script F 场景3预热后调用 bash F；应核验写池完成，成功返回0。
                      未提供时，须在交互终端手动输入yes后才进行正式测试。
--allow-length-drift  明确允许生成数据不是精确131072 token；结果标为近似长度。
--python EXE          默认 python3；应与 ais_bench 位于同一已激活环境。
--out-root DIR        默认 ./glm53_pool_runs。

使用已安装且已信任的工具副本。不能与其他AISBench/wrapper共用同一安装目录并发运行。
prepare产生的p10数据供场景2/3复用；A2/H20应复制同一整份数据目录。
详细说明见 glm53_pool_aisbench_README.md。
HELP
}

ACTION=${1:-help}
if [[ $# -gt 0 ]]; then shift; fi
TOOLS= TOKENIZER= DATA= AIS_ROOT= PLATFORM= SCENE= HOST= MODEL= SERVER_CONFIG=
PORT=8000 OUT_ROOT=./glm53_pool_runs PYTHON=python3 METRICS_URL= POOL_CHECK=
CACHE_READY=0 ALLOW_DRIFT=0
while (($#)); do
  case "$1" in
    --tools-dir|--tokenizer|--data-dir|--ais-root|--platform|--scene|--host|--port|--model|--server-config|--out-root|--python|--metrics-url|--pool-check-script)
      [[ $# -ge 2 && -n "$2" ]] || { echo "缺少参数值：$1" >&2; exit 2; }
      case "$1" in
        --tools-dir) TOOLS=$2;; --tokenizer) TOKENIZER=$2;; --data-dir) DATA=$2;;
        --ais-root) AIS_ROOT=$2;; --platform) PLATFORM=$2;; --scene) SCENE=$2;;
        --host) HOST=$2;; --port) PORT=$2;; --model) MODEL=$2;;
        --server-config) SERVER_CONFIG=$2;; --out-root) OUT_ROOT=$2;;
        --python) PYTHON=$2;; --metrics-url) METRICS_URL=$2;; --pool-check-script) POOL_CHECK=$2;;
      esac
      shift 2;;
    --cache-ready) CACHE_READY=1; shift;;
    --allow-length-drift) ALLOW_DRIFT=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2;;
  esac
done
case "$ACTION" in help|-h|--help) usage; exit 0;; prepare|check|run) ;; *) usage >&2; exit 2;; esac
fail() { echo "错误：$*" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || fail "找不到Python：$PYTHON"
[[ -d "$TOOLS" && -d "$TOKENIZER" ]] || fail "--tools-dir和--tokenizer必须是现有本地目录"
TOOLS=$(cd "$TOOLS" && pwd -P)
TOKENIZER=$(cd "$TOKENIZER" && pwd -P)
[[ -f "$TOOLS/aisbench_test.py" && -f "$TOOLS/generate_dataset.py" ]] || fail "工具目录缺少入口或生成器"
# 上游wrapper使用shell字符串拼命令；拒绝可能破坏引号的路径。
safe_path() { [[ "$1" =~ ^[a-zA-Z0-9_./:@+-]+$ ]] || fail "此上游wrapper不安全支持空格/特殊字符路径：$1"; }
safe_path "$TOOLS"; safe_path "$TOKENIZER"
mkdir -p "$OUT_ROOT"
OUT_ROOT=$(cd "$OUT_ROOT" && pwd -P)
safe_path "$OUT_ROOT"
SESSION=$(mktemp -d "$OUT_ROOT/${ACTION}_$(date +%Y%m%d_%H%M%S)_XXXXXX")
WORK="$SESSION/work"
mkdir "$WORK"
# 在运行目录操作副本；不改原工具配置和picked_ids.txt。
cp "$TOOLS"/*.py "$WORK/"
for name in GSM8K.jsonl picked_ids.txt; do
  if [[ -f "$TOOLS/$name" ]]; then cp "$TOOLS/$name" "$WORK/$name"; fi
done
export POOL_TOOLS="$TOOLS" POOL_WORK="$WORK" POOL_TOKENIZER="$TOKENIZER"
export POOL_SESSION="$SESSION" POOL_ALLOW_DRIFT="$ALLOW_DRIFT"
export POOL_HOST="$HOST" POOL_PORT="$PORT" POOL_MODEL="$MODEL"

cat > "$SESSION/helper.py" <<'PY'
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
from datetime import datetime, timezone

MARKER = "装订版本，work by claude code，2026-11-16"
TARGET = 131072

def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

def rows(path):
    result = []
    with Path(path).open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            r = json.loads(line)
            if not isinstance(r, dict) or not isinstance(r.get("question"), str):
                raise ValueError(f"{path}:{i}: 缺少字符串question")
            result.append(r)
    return result

def tokenizer():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(os.environ["POOL_TOKENIZER"], local_files_only=True)

def tokenizer_hashes():
    root = Path(os.environ["POOL_TOKENIZER"])
    return {p.name: sha(p) for p in root.iterdir()
            if p.is_file() and (p.name.startswith("tokeniz") or p.name in
                               ("special_tokens_map.json", "vocab.json", "merges.txt", "added_tokens.json"))}

def audit(root):
    tok = tokenizer()
    report = {"target_input_tokens": TARGET, "tokenizer": os.environ["POOL_TOKENIZER"],
              "token_count_method": "encode(add_special_tokens=True); actual server usage still required",
              "sets": {}, "attribution": MARKER}
    for pn in (1, 10):
        d = Path(root) / f"p{pn}"
        prefixes, full = rows(d / "prefix.jsonl"), rows(d / "full.jsonl")
        if len(prefixes) != pn or len(full) != 30:
            raise ValueError(f"p{pn}: 前缀/正式行数应为{pn}/30，实际{len(prefixes)}/{len(full)}")
        if len({r["question"] for r in prefixes}) != pn or len({r["question"] for r in full}) != 30:
            raise ValueError(f"p{pn}: 存在重复前缀或重复完整请求")
        prefix_ids = [tok.encode(r["question"], add_special_tokens=True) for r in prefixes]
        lengths, common_lengths = [], []
        for i, row in enumerate(full):
            p = prefixes[i % pn]["question"]
            if not row["question"].startswith(p):
                raise ValueError(f"p{pn} row {i}: 不符合上游循环前缀分组，需检查分支/生成器")
            ids = tok.encode(row["question"], add_special_tokens=True)
            pid = prefix_ids[i % pn]
            n = 0
            for a, b in zip(pid, ids):
                if a != b:
                    break
                n += 1
            if n == 0:
                raise ValueError(f"p{pn} row {i}: token级前缀没有匹配")
            lengths.append(len(ids))
            common_lengths.append(n)
        cross_lcp = 0
        for i, first in enumerate(prefix_ids):
            for second in prefix_ids[i + 1:]:
                n = 0
                for a, b in zip(first, second):
                    if a != b:
                        break
                    n += 1
                cross_lcp = max(cross_lcp, n)
        report["sets"][f"p{pn}"] = {
            "prefix_rows": pn, "full_rows": 30,
            "prefix_token_lengths": list(map(len, prefix_ids)),
            "input_token_lengths": lengths, "matched_prefix_tokens": common_lengths,
            "max_cross_prefix_common_tokens": cross_lcp,
            "exact_input_length": all(n == TARGET for n in lengths),
            "sha256": {name: sha(d / name) for name in ("prefix.jsonl", "full.jsonl")}}
    return report

def prepare(root):
    root = Path(root).resolve()
    if root.exists():
        raise ValueError("数据目录已存在，拒绝覆盖；请选一个新的--data-dir")
    root.mkdir(parents=True)
    work = Path(os.environ["POOL_WORK"])
    if not (work / "GSM8K.jsonl").is_file():
        raise ValueError("工具目录缺少GSM8K.jsonl；脚本不会自动下载")
    os.chdir(work)
    sys.path.insert(0, str(work))
    from generate_dataset import create_multi_prefix_dataset
    for pn in (1, 10):
        d = root / f"p{pn}"
        d.mkdir()
        p, f = create_multi_prefix_dataset(
            tokenizer_path=os.environ["POOL_TOKENIZER"], input_len=TARGET, number=30,
            save_path=str(d), prefix_flag=1, dp=1, repeat_rate=0.9, seed=1, prefix_num=pn)
        import shutil
        shutil.copyfile(p, d / "prefix.jsonl")
        shutil.copyfile(f, d / "full.jsonl")
    report = audit(root)
    report["tokenizer_files_sha256"] = tokenizer_hashes()
    save(root / "manifest.json", report)
    for name, value in report["sets"].items():
        vals = value["input_token_lengths"]
        print(f"{name}: input tokens min={min(vals)}, max={max(vals)}, exact={value['exact_input_length']}")
        if value["max_cross_prefix_common_tokens"] > TARGET // 10:
            print(f"警告：{name}不同前缀仍共享较长token，请检查manifest", file=sys.stderr)
    print("已冻结数据：", root)
    if not all(v["exact_input_length"] for v in report["sets"].values()):
        print("警告：生成器decode/re-encode导致长度漂移；run默认拒绝。"
              "可修正后重新prepare，或明确使用--allow-length-drift测近似128K。", file=sys.stderr)

def verify(root, pn):
    root = Path(root)
    old = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    current = audit(root)
    if old["tokenizer_files_sha256"] != tokenizer_hashes():
        raise ValueError("当前tokenizer文件与prepare时不同，拒绝跨平台混用")
    for name in ("p1", "p10"):
        for field in ("sha256", "input_token_lengths", "matched_prefix_tokens"):
            if old["sets"][name][field] != current["sets"][name][field]:
                raise ValueError(f"{name}的{field}与prepare时不同，拒绝继续")
    if not current["sets"][f"p{pn}"]["exact_input_length"] and os.environ["POOL_ALLOW_DRIFT"] != "1":
        raise ValueError("输入不是精确131072 token；查看manifest。若接受近似长度，显式加--allow-length-drift")
    save(Path(os.environ["POOL_SESSION"]) / "data_audit.json", current)

def check():
    import ais_bench
    from ais_bench.benchmark.models import VLLMCustomAPI, BaseAPIModel
    src = inspect.getsource(BaseAPIModel.generate)
    if not re.search(r"range\(\s*self\.retry\s*\)", src):
        raise ValueError("本版AISBench重试实现与已核对版不同，不能保证retry=1仅一次尝试；请先人工核对")
    root = Path(next(iter(ais_bench.__path__))).resolve().parent
    print(root)
    save(Path(os.environ["POOL_SESSION"]) / "client_environment.json", {
        "python": sys.version, "executable": sys.executable,
        "ais_root_imported": str(root), "retry_semantics": "range(self.retry); retry=1",
        "api_class": f"{VLLMCustomAPI.__module__}.{VLLMCustomAPI.__name__}",
        "tools_sha256": {p.name: sha(p) for p in Path(os.environ["POOL_TOOLS"]).glob("*.py")},
        "attribution": MARKER})

def execute(work, args):
    # 上游使用shell=True + tee，默认/bin/sh会吞掉ais_bench失败状态。
    original = subprocess.run
    def checked_run(*a, **kw):
        if kw.get("shell"):
            if not a or not isinstance(a[0], str):
                raise ValueError("未知的上游shell调用形式")
            a = ("set -o pipefail; " + a[0],) + a[1:]
            kw["executable"] = "/bin/bash"
        return original(*a, **kw)
    subprocess.run = checked_run
    os.chdir(work)
    sys.path.insert(0, work)
    sys.argv = [str(Path(work) / "aisbench_test.py")] + args
    runpy.run_path(sys.argv[0], run_name="__main__")

def counts(log, expected, out):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", Path(log).read_text(encoding="utf-8", errors="replace"))
    def values(label):
        result = []
        for line in text.splitlines():
            m = re.search(label, line, re.I)
            if m:
                tail = line[m.end():]
                tail = re.sub(r"\btotal\b", "", tail, flags=re.I)
                tail = tail.strip(" \t:：|│┃")
                if re.fullmatch(r"\d+", tail):
                    result.append(int(tail))
        return result
    success = values(r"\b(?:success|successful)[ _]+requests\b")
    failed = values(r"\bfailed[ _]+requests\b")
    ok = bool(success and failed) and all(n == expected for n in success) and all(n == 0 for n in failed)
    save(out, {"expected_success": expected, "parsed_success": success, "parsed_failed": failed,
               "request_counts_verified": ok,
               "actual_server_input_output_lengths_verified": False,
               "note": "仅核验客户端汇总计数；仍需核对逐请求usage、外部读取和服务端配置。",
               "attribution": MARKER})
    if not ok:
        raise ValueError("请求数未通过校验或本版报告格式无法解析。保留日志，请查原生报告，不能视作成功")

if __name__ == "__main__":
    try:
        mode, *args = sys.argv[1:]
        if mode == "prepare": prepare(args[0])
        elif mode == "verify": verify(args[0], int(args[1]))
        elif mode == "check": check()
        elif mode == "execute": execute(args[0], args[1:])
        elif mode == "counts": counts(args[0], int(args[1]), args[2])
        elif mode == "manifest":
            save(Path(os.environ["POOL_SESSION"]) / "run_manifest.json", {
                "utc": datetime.now(timezone.utc).isoformat(),
                "platform": os.environ["POOL_PLATFORM"], "scene": os.environ["POOL_SCENE"],
                "host": os.environ["POOL_HOST"], "port": os.environ["POOL_PORT"],
                "model": os.environ["POOL_MODEL"], "target": {
                    "input_tokens": TARGET, "output_tokens": 1, "prefix_ratio": 0.9,
                    "prefix_num": int(os.environ["POOL_PN"]), "concurrency": 10, "requests": 30,
                    "tp": 8, "dp": 1, "ep": 8, "mtp": False, "dcp": 1},
                "server_configuration": "operator_confirmed_only; archived, not automatically verified",
                "initial_cache_state": "operator_confirmed_isolated; no automatic cache reset",
                "pool_enabled_expected": os.environ["POOL_SCENE"] == "3",
                "allow_length_drift": os.environ["POOL_ALLOW_DRIFT"] == "1",
                "api": "VLLMCustomAPI, stream=True, /v1/completions",
                "attribution": MARKER})
        else: raise ValueError(f"未知内部动作：{mode}")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
PY

if [[ "$ACTION" == prepare ]]; then
  [[ -n "$DATA" ]] || fail "prepare需要--data-dir"
  "$PYTHON" -I "$SESSION/helper.py" prepare "$DATA" 2>&1 | tee "$SESSION/prepare.log"
  echo "prepare日志/生成器副本：$SESSION"
  exit 0
fi
command -v ais_bench >/dev/null || fail "当前PATH找不到ais_bench，请激活正确的Python环境"
command -v flock >/dev/null || fail "需要Linux flock以保护AISBench共享配置"
"$PYTHON" -I "$SESSION/helper.py" check > "$SESSION/imported_ais_root.txt"
IMPORTED_ROOT=$(tail -n 1 "$SESSION/imported_ais_root.txt")
if [[ -z "$AIS_ROOT" ]]; then AIS_ROOT=$IMPORTED_ROOT; fi
[[ -d "$AIS_ROOT/ais_bench" ]] || fail "--ais-root不是包含ais_bench的根目录"
AIS_ROOT=$(cd "$AIS_ROOT" && pwd -P)
safe_path "$AIS_ROOT"
[[ "$AIS_ROOT" == "$IMPORTED_ROOT" ]] || fail "--ais-root与当前Python导入位置不同，请统一Python/ais_bench环境"
export POOL_AIS_ROOT="$AIS_ROOT"
cat > "$WORK/config.py" <<'PY'
import os
DATASET_PATH = os.path.join(os.environ['POOL_SESSION'], 'generated_unused')
WORK_PATH = os.environ['POOL_AIS_ROOT']
MODEL_PATH = os.environ['POOL_TOKENIZER']
MODEL_NAME = os.environ['POOL_MODEL']
HOST_IP = os.environ['POOL_HOST']
HOST_PORT = os.environ['POOL_PORT']
URL = ''
API_KEY = ''
DEFAULT_PERFORMANCE_TEST = 'default_perf'
OUTPUT_DIR = os.path.join(os.environ.get('POOL_PHASE_DIR', os.environ['POOL_SESSION']), 'ais_outputs')
POD_INFO = [HOST_IP + ':' + HOST_PORT]
# 装订版本，work by claude code，2026-11-16
PY
{ command -v ais_bench; "$PYTHON" -m pip show ais-bench-benchmark transformers; } > "$SESSION/package_versions.txt" 2>&1
if [[ "$ACTION" == check ]]; then
  "$PYTHON" -I "$SESSION/helper.py" execute "$WORK" --help > "$SESSION/wrapper_help.txt" 2>&1
  echo "依赖/接口检查完成（未访问推理服务）。日志：$SESSION"
  exit 0
fi

[[ "$PLATFORM" == A2 || "$PLATFORM" == H20 ]] || fail "--platform必须为A2或H20"
[[ "$SCENE" =~ ^[123]$ ]] || fail "--scene必须为1/2/3"
[[ -n "$HOST" && "$HOST" =~ ^[a-zA-Z0-9_.-]+$ ]] || fail "--host填写IPv4或主机名，不带http://和路径"
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] && ((10#$PORT > 0 && 10#$PORT <= 65535)) || fail "无效端口"
[[ -n "$MODEL" ]] || fail "需要--model，与服务端served-model-name一致"
[[ "$CACHE_READY" == 1 ]] || fail "先隔离旧缓存并确认服务配置，再加--cache-ready"
[[ -f "$SERVER_CONFIG" ]] || fail "需要--server-config保存的实际启动命令/日志文件"
[[ -d "$DATA" ]] || fail "先运行prepare，再传--data-dir"
DATA=$(cd "$DATA" && pwd -P)
safe_path "$DATA"
PN=10; if [[ "$SCENE" == 1 ]]; then PN=1; fi
if [[ "$SCENE" == 3 ]]; then
  if [[ -n "$POOL_CHECK" ]]; then
    [[ -f "$POOL_CHECK" ]] || fail "--pool-check-script文件不存在"
    POOL_CHECK=$(cd "$(dirname "$POOL_CHECK")" && pwd -P)/$(basename "$POOL_CHECK")
  else
    [[ -t 0 ]] || fail "场景3非交互运行必须提供--pool-check-script，不能默认为写池已完成"
  fi
fi
export POOL_PLATFORM="$PLATFORM" POOL_SCENE="$SCENE" POOL_PN="$PN"
"$PYTHON" -I "$SESSION/helper.py" verify "$DATA" "$PN"
"$PYTHON" -I "$SESSION/helper.py" manifest
cp "$SERVER_CONFIG" "$SESSION/server_config.txt"
cp "$DATA/manifest.json" "$SESSION/dataset_manifest.json"
METRICS_URL=${METRICS_URL:-http://$HOST:$PORT/metrics}
command -v curl >/dev/null || fail "需要curl采集metrics"

# wrapper会改写共享路径；加锁、快照，在正常/异常退出时恢复。
exec 9> "$AIS_ROOT/.glm53_pool_aisbench.lock"
flock -n 9 || fail "另一个本脚本实例正在使用同一AISBench目录"
mkdir "$SESSION/backup"
TARGETS=("$AIS_ROOT/ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py"
         "$AIS_ROOT/ais_bench/datasets/gsm8k/test.jsonl"
         "$AIS_ROOT/ais_bench/datasets/gsm8k/train.jsonl")
SAVED=0
restore() {
  local rc=$? i target restore_failed=0
  trap - EXIT INT TERM
  for ((i=0; i<SAVED; i++)); do
    target=${TARGETS[$i]}
    if [[ -e "$SESSION/backup/$i" || -L "$SESSION/backup/$i" ]]; then
      rm -f -- "$target" && cp -a -- "$SESSION/backup/$i" "$target" || restore_failed=1
    else
      rm -f -- "$target" || restore_failed=1
    fi
  done
  if ((restore_failed)); then echo "警告：恢复AISBench文件失败，请用$SESSION/backup人工恢复" >&2; rc=1; fi
  printf 'exit_code=%s\n' "$rc" > "$SESSION/exit_status.txt"
  echo "实验目录：$SESSION"
  exit "$rc"
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for i in "${!TARGETS[@]}"; do
  target=${TARGETS[$i]}
  [[ -d "$(dirname "$target")" ]] || fail "AISBench缺少预期目录：$(dirname "$target")"
  [[ ! -d "$target" ]] || fail "不能替换目录：$target"
  if [[ -e "$target" || -L "$target" ]]; then cp -a -- "$target" "$SESSION/backup/$i"; fi
  SAVED=$((SAVED + 1))
done

cat > "$WORK/default_api.py" <<'PY'
import os
from ais_bench.benchmark.models import VLLMCustomAPI
models = [dict(
    attr='service', type=VLLMCustomAPI, stream=True,
    abbr='glm53-pool-completion-stream',
    path=os.environ['POOL_TOKENIZER'], model=os.environ['POOL_MODEL'], api_key='',
    host_ip=os.environ['POOL_HOST'], host_port=int(os.environ['POOL_PORT']), url='',
    request_rate=rr_for_replace, retry=1,
    max_out_len=outputlen_for_replace, batch_size=concurrency_for_replace,
    generation_kwargs=dict(temperature=0, ignore_eos=True, min_tokens=1),
)]
# 装订版本，work by claude code，2026-11-16
PY

metrics() {
  curl --fail --silent --show-error --connect-timeout 10 --max-time 60 "$METRICS_URL" > "$1"
}
run_phase() {
  local phase=$1 file=$2 count=$3 concurrency=$4
  export POOL_PHASE_DIR="$SESSION/$phase"
  mkdir "$POOL_PHASE_DIR" || return
  local args=(--dataset "$file" --dataset_type normal
    --input_len 131072 --output_len 1 --data_num "$count"
    --concurrency "$concurrency" --request_rate 0 --test_type stream
    --repeat 1 --npu_num 8)
  # 现成数据不需要prefix_cache生成分支，normal避免依赖wrapper命中率解析器。
  printf '%q ' "$PYTHON" aisbench_test.py "${args[@]}" > "$POOL_PHASE_DIR/command.txt"
  printf '\n' >> "$POOL_PHASE_DIR/command.txt"
  echo "开始 $phase：$count 条，并发 $concurrency；日志 $POOL_PHASE_DIR/console.log"
  local rc=0
  "$PYTHON" -I "$SESSION/helper.py" execute "$WORK" "${args[@]}" \
    > "$POOL_PHASE_DIR/console.log" 2>&1 || rc=$?
  for f in temp_api.py config.py default_api.py aisbench.log aisbench_all.log aisbench_result.csv; do
    if [[ -f "$WORK/$f" ]]; then cp "$WORK/$f" "$POOL_PHASE_DIR/$f" || return; fi
  done
  if ((rc)); then tail -n 40 "$POOL_PHASE_DIR/console.log" >&2; return "$rc"; fi
  "$PYTHON" -I "$SESSION/helper.py" counts "$POOL_PHASE_DIR/console.log" "$count" "$POOL_PHASE_DIR/request_counts.json"
}

metrics "$SESSION/before_warmup.metrics"
run_phase warmup "$DATA/p$PN/prefix.jsonl" "$PN" 1
if [[ "$SCENE" == 3 ]]; then
  if [[ -n "$POOL_CHECK" ]]; then
    echo "执行写池验收脚本：$POOL_CHECK"
    cp "$POOL_CHECK" "$SESSION/pool_check_script.sh"
    POOL_RUN_DIR="$SESSION" bash "$POOL_CHECK" > "$SESSION/pool_check.log" 2>&1
    printf 'source=operator_supplied_check_script\n' > "$SESSION/pool_write_gate.txt"
  else
    echo "请检查Mooncake已写入本轮前缀、无失败/回退、引擎空闲。脚本不会自动证明这些状态。"
    read -r -p '确认后输入yes继续正式30条测试：' answer
    [[ "$answer" == yes ]] || fail "未确认写池完成，停止"
    printf 'source=manual_operator_confirmation_not_automatic_verification\n' > "$SESSION/pool_write_gate.txt"
  fi
fi
metrics "$SESSION/before_measure.metrics"
rc=0
run_phase measure "$DATA/p$PN/full.jsonl" 30 10 || rc=$?
if ! metrics "$SESSION/after_measure.metrics"; then
  echo "错误：结束metrics采集失败，无法完整计算命中增量" >&2
  if ((rc == 0)); then rc=1; fi
fi
((rc == 0)) || exit "$rc"
echo "执行完成：预热与正式请求数校验通过。"
echo "仍需核验：逐请求实际输入/输出token、TTFT、池读取字节、回退和服务端配置。"
echo "输出1 token，不使用TPOT作为结论；原始AISBench报告在各阶段ais_outputs下。"
