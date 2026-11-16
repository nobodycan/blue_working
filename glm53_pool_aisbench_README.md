# GLM-5.3 池化实验：AISBench Shell 脚本

配套脚本：`glm53_pool_aisbench.sh`。用于 Linux Bash 4+，依赖已经安装、已信任的 `aisbench_auto_tools_prefix` 和 AISBench。按 `rayn-zzz/aisbench_auto_tools_prefix` 的接口实现，不自动安装/升级依赖。

## 1. 固定实验口径

| 场景 | Mooncake池 | 前缀数 | 构造前缀比例 | 正式输入 | 输出 | 客户端并发 | 正式条数 |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | 关闭 | 1 | 0.9 | 131072 token | 1 | 10 | 30 |
| 2 | 关闭 | 10 | 0.9 | 131072 token | 1 | 10 | 30 |
| 3 | 开启 | 10 | 0.9 | 131072 token | 1 | 10 | 30 |

- TP8、DP1、EP8、不开MTP、DCP=1、开启prefix cache：由操作者在服务端配置并核对，脚本不修改这些配置。
- 假设是单实例P/D混合推理加KV池，不是P/D分离。
- 预热单独执行：并发1、输出1，前缀数分别1/10/10；不计入正式30条。
- 主测不主动清预热后的HBM。场景3可能继续命中HBM，不保证一定读池。
- 使用流式 `/v1/completions`，避免额外Chat模板；服务需要支持该接口、`ignore_eos`、`min_tokens`。
- 使用 `default_perf`、`--repeat 1`，不使用 `stable_stage` 或 `--prefix_test`。
- 两阶段都使用现成文件、`dataset_type=normal`；该参数不删除数据自带前缀，也不关闭服务端缓存。
- 当前脚本用于无鉴权的内部推理与metrics端点，不读取/保存API密钥。

## 2. 准备工作

在压测客户端激活AISBench环境，确保以下命令可用：

```bash
python3 --version
ais_bench --help
command -v flock
command -v curl
```

工具目录应包含上游的 `.py` 文件及 `GSM8K.jsonl`。Tokenizer必须是本地目录、与服务端一致；不启用 `trust_remote_code`。路径使用英文/数字/下划线等，不含空格或Shell特殊字符。

把脚本和本说明放在压测客户端任意目录，例如 `/opt/pool_test`。以下示例均在该目录执行：

```bash
cd /opt/pool_test

COMMON=(
  --tools-dir /opt/aisbench_auto_tools_prefix
  --tokenizer /models/GLM-5.3
  --out-root /data/glm53_pool_runs
)
DATA=/data/glm53_pool_data_r1
```

`--tokenizer`不要求客户端有完整权重，但需要完整、可用且一致的tokenizer文件。脚本不会自动下载模型或数据。

## 3. 检查客户端

```bash
bash glm53_pool_aisbench.sh check "${COMMON[@]}"
```

自动发现当前Python导入的AISBench根目录。可以显式提供 `--ais-root`，但必须与当前Python一致。检查重试语义是否支持 `retry=1`表示一次尝试；不符合则停止，而不是猜测。

Python和PATH中的 `ais_bench` 必须来自同一环境；脚本记录两者位置，但不能保证所有外部入口的环境一致。

## 4. 只生成一次数据

```bash
bash glm53_pool_aisbench.sh prepare "${COMMON[@]}" --data-dir "$DATA"
```

`--data-dir`必须是一个不存在的新目录，避免覆盖旧数据。

输出：

```text
/data/glm53_pool_data_r1/
├── p1/
│   ├── prefix.jsonl      # 1条
│   └── full.jsonl        # 30条
├── p10/
│   ├── prefix.jsonl      # 10条
│   └── full.jsonl        # 30条，每个前缀3条
└── manifest.json        # 哈希、token长度、匹配前缀长度、跨前缀公共长度
```

目录中还会保留上游生成器原始命名的数据文件。原始工具的 `picked_ids.txt` 在工作副本中使用，不修改原工具目录。

**场景2、3复用整个p10；A2/H20复用同一整份数据目录，不各自生成一份。** 复制时带上manifest，不改动文件内容。

### 输入长度漂移

上游生成器经过decode/re-encode，目标131072不代表最终恰好131072。脚本会用当前tokenizer逐条计数，并记录真实前缀匹配长度。

- 数据生成可完成，但长度不精确时会警告。
- `run`默认拒绝非精确长度，不会悄悄当作严格128K。
- 若接受“近似128K”，运行时显式添加 `--allow-length-drift`；结果会记录该选择。
- 若需要严格长度，请修正所用生成器后使用新目录重新prepare，不要手动改文件后绕过哈希检查。
- 本地计数采用 `add_special_tokens=True`。仍需核对服务端usage和已安装AISBench的数据集模板，最终以实际发送/接收为准。

## 5. 执行A2的三个场景

每组先在服务端完成对应配置并隔离旧缓存，确认引擎空闲。把实际启动命令/参数或日志保存为本地文本文件供脚本归档；不要包含密钥。

### A2 场景1

服务端关闭KV池，开启本地prefix cache，确认TP8/DP1/EP8、MTP关闭、DCP=1：

```bash
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --data-dir "$DATA" --platform A2 --scene 1 \
  --host 192.168.1.10 --port 8000 --model glm53 \
  --server-config /data/a2_no_pool_actual.txt --cache-ready
```

### A2 场景2

服务端仍无池，但须重新隔离场景1旧缓存：

```bash
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --data-dir "$DATA" --platform A2 --scene 2 \
  --host 192.168.1.10 --port 8000 --model glm53 \
  --server-config /data/a2_no_pool_actual.txt --cache-ready
```

### A2 场景3

先在服务端开启已验证的Mooncake池化连接器，保持其余配置不变，隔离旧池对象与本地缓存：

```bash
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --data-dir "$DATA" --platform A2 --scene 3 \
  --host 192.168.1.10 --port 8000 --model glm53 \
  --server-config /data/a2_pool_actual.txt --cache-ready
```

预热10条后脚本暂停，检查写池完成、无错误/回退、引擎空闲，再输入 `yes`，开始正式30条。

**`--cache-ready`是操作者确认，不会真的清缓存。** 脚本也不自动证明提供的启动配置已生效。

## 6. 执行H20

使用同一数据集与同样三条命令，分别改为：

```text
--platform H20
--host 实际H20推理服务IP
--port 实际端口
--model 实际served-model-name
--server-config H20对应场景实际启动参数文件
```

例如H20场景3：

```bash
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --data-dir "$DATA" --platform H20 --scene 3 \
  --host 192.168.1.20 --port 8000 --model glm53 \
  --server-config /data/h20_pool_actual.txt --cache-ready
```

`--platform`只是实验标签，不会选择硬件或改变服务。A2/H20的镜像、权重和KV dtype由各自实际适配决定，不能仅因测试参数相同就认为量化条件相同。

## 7. 场景3无人值守

可以提供真实的池状态检查脚本：

```bash
bash glm53_pool_aisbench.sh run "${COMMON[@]}" \
  --data-dir "$DATA" --platform A2 --scene 3 \
  --host 192.168.1.10 --port 8000 --model glm53 \
  --server-config /data/a2_pool_actual.txt --cache-ready \
  --pool-check-script /opt/pool_test/check_pool_written.sh
```

脚本在预热结束后通过 `bash` 调用检查脚本；检查脚本须确认本轮前缀写池完成，成功返回0，否则返回非0停止。可以通过环境变量 `POOL_RUN_DIR` 读取本次实验目录。不要用无条件 `exit 0`冒充验证。

非交互执行场景3却未提供检查脚本时，会在发请求前拒绝。

## 8. 结果和失败处理

每次调用创建一个新目录。`run`目录主要有：

```text
run_时间_随机标识/
├── run_manifest.json
├── data_audit.json
├── dataset_manifest.json
├── server_config.txt
├── client_environment.json
├── package_versions.txt
├── before_warmup.metrics
├── before_measure.metrics
├── after_measure.metrics
├── pool_write_gate.txt          # 仅场景3
├── warmup/
│   ├── console.log
│   ├── request_counts.json
│   ├── temp_api.py
│   └── ais_outputs/
├── measure/
│   ├── console.log
│   ├── request_counts.json
│   ├── temp_api.py
│   └── ais_outputs/
├── backup/                     # AISBench被临时替换路径的原件
└── exit_status.txt
```

- 正式统计只看 `measure`，不要混入 `warmup`。
- 汇总计数要求预热成功1/10条、主测成功30条、失败0条；统计表无法解析时也停止，不把未知当成功。
- wrapper内部 `tee`管道启用pipefail，避免底层AISBench失败被吞掉。
- 原始AISBench结果完整保留；不把wrapper附加CSV当唯一依据。
- 采集原始metrics，不根据固定标签猜命中率，也不自动将本地和external命中相加。
- `exit_code=0`只代表流程及汇总请求数检查完成，不证明实际输入/输出长度、池读取或服务配置正确。
- 输出1 token，不使用TPOT作为结论；主要看TTFT、完成时间、真实池读取字节、失败/回退和抢占。

若场景3没有实际池读取，不能称为池回读性能测试。需要额外做HBM冷/池热验收，并与主测分开标注；本脚本不擅自清缓存或重启服务。

## 9. 共享目录保护

原工具目录不改；在私有运行目录生成临时config与接口模板。AISBench的临时模型配置、`test.jsonl`、`train.jsonl`会按上游行为临时创建/替换，脚本先备份并在正常退出、错误、SIGINT/SIGTERM时尝试恢复。

- 同一个AISBench目录只允许一个本脚本实例，使用flock锁。
- 不要同时运行其他未遵循该锁的AISBench/wrapper进程。
- SIGKILL、断电、容器强制销毁无法触发恢复；用运行目录中的backup和记录路径人工恢复。
- 不自动清理实验目录，保留原始数据和日志。

## 10. 本地离线验证

```bash
bash -n glm53_pool_aisbench.sh
python3 -I glm53_pool_aisbench_test.py
```

离线测试只验证脚本逻辑，不代表已经在真实A2/H20、GLM-5.3、Mooncake和AISBench上完成联调。

> 装订版本，work by claude code，2026-11-16
