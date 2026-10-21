from pathlib import Path
root=Path(r'D:\work\ascend_2node_pd_128k_1k_2026-10-21')
p=root/'README-body.md';s=p.read_text(encoding='utf-8')
needle='同时交付 `B_D(t)` 的分布（至少 min/P50/P95/max 和迭代加权均值）、P/D waiting、KV 使用率曲线、抢占计数和 NPU HBM。持续负载有新 KV 到达，D 侧不是每一秒都属于本手册的纯 D 窗口。'
replacement=needle+'''

把本轮客户端 summary 复制到 D 结果目录，生成真实调度 batch 分布：

```bash
# P 宿主机
scp /data/v4flash-2node/results/steady-B8-1.json \\
  USER@10.10.0.12:/data/v4flash-2node/results/
# D 宿主机
docker exec "$CONTAINER" python /bench/scripts/report_batches.py \\
  --steps /bench/results/steps/pd-D --summary /bench/results/steady-B8-1.json \\
  --out /bench/results/steady-B8-1-actual-batches.json
```

该分布按实际模型调度迭代统计，不包含无任务时没有 execute_model 事件的空闲时间；空闲占比和时间平均在途量要由 running/waiting 指标补充。P/D 两台不同 worker 的 batch 不相加。
'''
s=s.replace(needle,replacement)
s=s.replace('两台分别持续运行 `host_monitor.sh`，保存 `P-pd.log` / `D-pd.log`。','两台分别持续运行 HBM 监控（将 ROLE 分别填 P、D）：\n\n```bash\nROLE=P\nnohup bash "$BENCH_ROOT/scripts/host_monitor.sh" \\\n  "$BENCH_ROOT/results/hbm/${ROLE}-pd.log" \\\n  > "$BENCH_ROOT/logs/hbm-${ROLE}-pd.log" 2>&1 &\necho $! > "$BENCH_ROOT/hbm-${ROLE}-pd.pid"\n```\n\n')
s=s.replace('指标里先查看实际名称和标签','指标里先查看实际名称和标签')
# Put a precise alternative for digest setting and service-readiness wait.
needle='模型 ready 后在 P 重新启动代理，按第 9 节热身。'
s=s.replace(needle,'''模型 ready 后在 P 重新启动代理：

```bash
nohup docker exec -e PROXY_AUDIT=1 "$CONTAINER" \\
  bash /bench/scripts/start_proxy.sh > "$BENCH_ROOT/logs/profile-proxy.log" 2>&1 &
curl -fsS http://10.10.0.11:1999/healthcheck
```

按第 9 节热身。''')
# Add failure preservation and formal probes-disabled paired measurements.
needle='## 12. 结果记录模板和验收'
extra='''### 11.4 关闭逐步探针与代理诊断的正式配对复测

先记录诊断结果、等待请求完全结束，再重启两台测试容器。按第 8 节重启服务时使用：

```bash
# P 宿主机
source /data/v4flash-2node/env.sh
docker restart "$CONTAINER"
nohup docker exec -e PROFILE=0 -e AUDIT_STEP_DIR= -e AUDIT_TRANSFER_DIR= \\
  "$CONTAINER" bash /bench/scripts/serve.sh prefill \\
  > "$BENCH_ROOT/logs/formal-P.log" 2>&1 &
# D 宿主机
source /data/v4flash-2node/env.sh
docker restart "$CONTAINER"
nohup docker exec -e PROFILE=0 -e AUDIT_STEP_DIR= -e AUDIT_TRANSFER_DIR= \\
  "$CONTAINER" bash /bench/scripts/serve.sh decode \\
  > "$BENCH_ROOT/logs/formal-D.log" 2>&1 &
# P 模型健康后，重新启动不带 HTTP 诊断包装的代理
nohup docker exec -e PROXY_AUDIT=0 "$CONTAINER" \\
  bash /bench/scripts/start_proxy.sh > "$BENCH_ROOT/logs/formal-proxy.log" 2>&1 &
```

重新热身，重跑 C=1 和 C=B 的正式波次，使用新的结果前缀如 `formal-single-*` / `formal-batch-B8-*`；用同一个 compare 脚本指定对应 glob。正式/诊断均保留版本和参数。若形态或吞吐变化超过约定阈值，要说明观测开销，不能把正式 TTFT 与诊断 TTFT 混在同一均值里。

停止本次 HBM 监控可以用保存的 PID：`kill -TERM "$(cat /data/v4flash-2node/hbm-monitor.pid)"`。保存失败点日志；每次复跑使用新的结果名，不要覆盖原始失败证据。

'''
s=s.replace(needle,extra+needle)
p.write_text(s,encoding='utf-8',newline='\n')
print('Manual body bytes:',p.stat().st_size)
