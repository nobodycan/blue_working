#!/usr/bin/env bash
# 装订版本，work by claude code，2026-11-18
# 在 aisbench_auto_tools_prefix 目录运行：bash /path/pd_qps_sweep.sh
# 只访问 proxy，不启动/停止 P、D，不清全局缓存。
# 每个选中的场景至少尝试一次正式压测；单场景失败不阻断后续场景。
set -euo pipefail
umask 077
exec "${PYTHON:-python3}" -I - "$@" <<'PD_QPS_PYTHON'
import argparse
import ast
import csv
try:
    import fcntl
except ImportError:
    fcntl = None  # --self-test可在Windows运行，真实压测只支持Linux。
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

VERSION = '2026-11-18'
STAMP = '装订版本，work by claude code，' + VERSION
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
NUMBER = re.compile(r'[-+]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?')
SCENARIOS = [('P',4096,1,1000), ('P',16384,1,2000), ('P',65536,1,8000),
             ('D',4096,2048,20), ('D',16384,1024,20), ('D',65536,1024,20)]
FIELDS = ['time','kind','input_len','output_len','concurrency','data_num','mode',
          'stage','status','sla_pass','selection_eligible','ttft_mean_ms','tpot_mean_ms',
          'qps','qps_source','scope_success','scope_failed','total_success','total_failed',
          'completion_scope','duration_s','input_tokens_mean','output_tokens_mean',
          'output_tps','actual_concurrency','proxy_cache_hit_pct','cache_evidence',
          'proxy_max_kv_usage','proxy_preempt_delta','warning','reason','cell_dir']

def norm(s):
    return re.sub(r'[\s_\-:/()\[\]]+', '', s).lower()

def numeric(s):
    m = NUMBER.search(s)
    return float(m.group().replace(',','')) if m else None

def milliseconds(s, header=''):
    n = numeric(s)
    if n is None:
        return None
    unit = s.lower() + ' ' + header.lower()
    if 'µs' in unit or 'μs' in unit or re.search(r'\bus\b',unit):
        return n / 1000
    if 'ms' in unit or '毫秒' in unit:
        return n
    if re.search(r'\bs\b|second|秒',unit):
        return n * 1000
    raise ValueError('时延指标缺少单位，拒绝猜测：' + s + ' / ' + header)

ALIASES = {
 'ttft': 'ttft_mean_ms', 'tpot':'tpot_mean_ms',
 'requestthroughput':'qps', 'requestspersecond':'qps',
 'successrequests':'success', 'successfulrequests':'success',
 'failedrequests':'failed', 'totalrequests':'requests',
 'benchmarkduration':'duration_s', 'inputtokens':'input_tokens_mean',
 'outputtokens':'output_tokens_mean', 'outputtokenthroughput':'output_tps',
 'concurrency':'actual_concurrency',
}

def parse_report(text, mode, stable_labels):
    """Parse table columns, never the helper's 99999-filled dictionary.
    stable_stage requires an explicit matching Stage; never fall back to total.
    """
    header = None
    stages = {}
    for line in ANSI.sub('',text).splitlines():
        if '│' in line:
            cells = [s.strip() for s in line[line.index('│'):].split('│')[1:-1]]
        elif '|' in line:
            cells = [s.strip() for s in line[line.index('|'):].split('|')[1:-1]]
        else:
            continue
        if len(cells) < 2:
            continue
        ns = [norm(c) for c in cells]
        if ns[0] in {'metric','commonmetric','metrics','performanceparameters','performanceparameter','指标','公共指标'}:
            header = cells
            continue
        if header is None or len(cells) != len(header):
            continue
        key = ALIASES.get(norm(re.sub(r'\s*[\[(].*?[\])]\s*$', '', cells[0])))
        if key is None:
            continue
        hn = [norm(c) for c in header]
        si = next((i for i,h in enumerate(hn) if h in {'stage','阶段'}), None)
        stage = cells[si] if si is not None else 'NO_STAGE'
        record = stages.setdefault(stage,{})
        vi = next((i for i,h in enumerate(hn) if h in {'value','average','avg','mean','均值','平均值'} or h.startswith(('average','mean','avg'))), None)
        if vi is None:
            continue
        val = cells[vi]
        try:
            if key in {'ttft_mean_ms','tpot_mean_ms'}:
                parsed = milliseconds(val, cells[0]+' '+header[vi])
            elif key == 'duration_s':
                parsed = milliseconds(val, cells[0]+' '+header[vi]) / 1000
            else:
                parsed = numeric(val)
        except (ValueError, TypeError):
            continue
        if parsed is not None and math.isfinite(parsed):
            record[key] = parsed
    total_candidates = [(s,d) for s,d in stages.items() if norm(s) in {'total','all','overall','全量','全部'}]
    total = total_candidates[-1][1] if total_candidates else {}
    if mode == 'default_perf':
        choices = total_candidates
        if not choices:
            choices = [(s,d) for s,d in stages.items() if s=='NO_STAGE']
    else:
        labels = {norm(x) for x in stable_labels.split(',') if x.strip()}
        choices = [(s,d) for s,d in stages.items() if norm(s) in labels]
    if not choices:
        raise ValueError('未找到目标统计阶段；已见阶段='+','.join(stages)+
                         '。stable_stage不回退到total，请检查raw.log与安装版本的Stage标签。')
    stage, data = choices[-1]
    data = dict(data)
    needed = ['ttft_mean_ms' if mode=='default_perf' else 'tpot_mean_ms','success','failed']
    missing = [k for k in needed if k not in data]
    if missing:
        raise ValueError('目标阶段缺少必需指标：'+','.join(missing))
    if 'qps' in data:
        data['qps_source'] = 'AISBench Request Throughput ['+stage+']'
    elif data.get('duration_s',0)>0:
        data['qps'] = data['success']/data['duration_s']
        data['qps_source'] = 'success/duration，同一阶段 ['+stage+']'
    else:
        raise ValueError('目标阶段缺少QPS及对应时长，拒绝用全程时长替代稳态时长')
    data['stage'] = stage
    data['total_success'] = total.get('success')
    data['total_failed'] = total.get('failed')
    return data

# Run existing code in a fresh private working copy; no global config edits.
CHILD = r'''
import json, os, sys, uuid
from pathlib import Path
work, kind, ilen, olen, cc, count, prompt_cache = sys.argv[1:]
work=Path(work); ilen=int(ilen); cc=int(cc); count=int(count)
os.chdir(work); sys.path.insert(0,str(work))
import config
cache=Path(prompt_cache)
tok=None
if kind=='P' or not cache.exists():
    from transformers import AutoTokenizer
    tok=AutoTokenizer.from_pretrained(config.MODEL_PATH,local_files_only=True)
    corpus=[json.loads(s)['question'] for s in (work/'GSM8K.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    if not corpus: raise RuntimeError('Empty GSM8K corpus')

def sized(body, nonce=''):
    prefix=tok.encode(nonce,add_special_tokens=False)
    ids=tok.encode(body,add_special_tokens=False)
    if not ids: raise RuntimeError('Empty tokenized body')
    n=ilen-len(prefix)
    if n<=0: raise RuntimeError('Input length shorter than nonce')
    ids=prefix+(ids*((n+len(ids)-1)//len(ids)))[:n]
    for _ in range(8):
        text=tok.decode(ids,skip_special_tokens=True)
        check=tok.encode(text,add_special_tokens=False)
        if len(check)==ilen: return text
        if len(check)>ilen: ids=check[:ilen]
        else: ids=check+(tok.encode(' test',add_special_tokens=False)*ilen)[:ilen-len(check)]
    raise RuntimeError('Tokenizer round-trip cannot produce exact input length')

if kind=='D' and not cache.exists():
    cache.write_text(sized(corpus[0]),encoding='utf-8')
path=work/'requests.jsonl'
with path.open('w',encoding='utf-8') as f:
    shared=cache.read_text(encoding='utf-8') if kind=='D' else None
    batch=uuid.uuid4().hex
    for i in range(count):
        prompt=shared if kind=='D' else sized(corpus[i%len(corpus)],'Request '+batch+' '+str(i)+'\n')
        f.write(json.dumps({'question':prompt,'answer':'none'},ensure_ascii=False)+'\n')
# Existing tool has ais_bench | tee, whose exit status would hide failures.
source=(work/'aisbench_test.py').read_text(encoding='utf-8')
needle='ais_bench_cmd = generate_aisbench_command(DEFAULT_PERFORMANCE_TEST)'
if source.count(needle)!=1: raise RuntimeError('Unsupported aisbench_test.py command generation layout')
source=source.replace(needle,needle+'\n    ais_bench_cmd = "bash -o pipefail -c " + __import__("shlex").quote(ais_bench_cmd)')
sys.argv=['aisbench_test.py','--input_len',str(ilen),'--output_len',olen,
 '--data_num',str(count),'--concurrency',str(cc),'--request_rate','0',
 '--dataset',str(path),'--dataset_type','normal','--repeat_rate','1.0' if kind=='D' else '0',
 '--dp','1','--npu_num','1']
exec(compile(source,str(work/'aisbench_test.py'),'exec'),{'__name__':'__main__','__file__':str(work/'aisbench_test.py')})
'''

def read_config(path):
    values = {}
    for node in ast.parse(path.read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError):
                continue
            for target in node.targets:
                if isinstance(target,ast.Name):
                    values[target.id] = value
    for key in ['WORK_PATH','MODEL_PATH','MODEL_NAME']:
        if not isinstance(values.get(key),str) or not values[key]:
            raise ValueError('config.py需要字面量字符串配置：'+key)
    return values

class SharedLinks:
    """Restore the three WORK_PATH entries the existing tool can modify."""
    def __init__(self, work, backup):
        self.backup = backup
        self.entries = []
        for rel in ['ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py',
                    'ais_bench/datasets/gsm8k/test.jsonl',
                    'ais_bench/datasets/gsm8k/train.jsonl']:
            p = work/rel
            p.parent.mkdir(parents=True,exist_ok=True)
            if p.is_symlink():
                saved = ('link', os.readlink(p))
            elif p.exists():
                if not p.is_file():
                    raise ValueError('无法备份非普通文件：'+str(p))
                dest = backup/str(len(self.entries))
                shutil.copy2(p,dest)
                saved = ('file',dest)
            else:
                saved = ('absent',None)
            self.entries.append((p,saved))
    def restore(self):
        errors=[]
        for p,(kind,value) in reversed(self.entries):
            try:
                if p.is_symlink() or p.is_file():
                    p.unlink()
                if kind=='link':
                    p.symlink_to(value)
                elif kind=='file':
                    shutil.copy2(value,p)
            except OSError as e:
                errors.append(str(p)+': '+str(e))
        if errors:
            raise RuntimeError('共享链接恢复失败；请使用备份人工恢复：'+'; '.join(errors))

class ProxyMetrics:
    def __init__(self, url, key):
        self.url=url
        self.key=key
        self.opener=build_opener(ProxyHandler({}))
    def fetch(self):
        try:
            headers={'Authorization':'Bearer '+self.key} if self.key else {}
            with self.opener.open(Request(self.url,headers=headers),timeout=3) as response:
                text=response.read(4*1024*1024).decode('utf-8','replace')
            values={}
            for line in text.splitlines():
                if not line or line.startswith('#'):
                    continue
                m=re.match(r'([^\s{]+)(?:\{.*\})?\s+([-+\d.eE]+)(?:\s|$)',line)
                if not m:
                    continue
                name,value=m.group(1),float(m.group(2))
                if math.isfinite(value):
                    values.setdefault(name,[]).append(value)
            result={}
            for name in ['vllm:prefix_cache_queries_total','vllm:prefix_cache_hits_total','vllm:num_preemptions_total']:
                if name in values:
                    result[name]=sum(values[name])
            if 'vllm:kv_cache_usage_perc' in values:
                result['kv']=max(values['vllm:kv_cache_usage_perc'])
            return result
        except Exception:
            return {}

class Monitor:
    def __init__(self, metrics):
        self.metrics=metrics
        self.stop=threading.Event()
        self.samples=[]
        self.before=metrics.fetch()
        self.thread=threading.Thread(target=self.loop,daemon=True)
    def loop(self):
        while not self.stop.wait(1):
            self.samples.append(self.metrics.fetch())
    def __enter__(self):
        self.thread.start()
        return self
    def __exit__(self,*unused):
        self.stop.set()
        self.thread.join(5)
        self.after=self.metrics.fetch()
        self.samples.extend([self.before,self.after])
    def result(self):
        def delta(name):
            a,b=self.before.get(name),self.after.get(name)
            return b-a if a is not None and b is not None and b>=a else None
        q=delta('vllm:prefix_cache_queries_total')
        h=delta('vllm:prefix_cache_hits_total')
        hit=100*h/q if q is not None and q>0 and h is not None and 0<=h<=q else None
        ks=[s['kv'] for s in self.samples if 'kv' in s]
        return {'proxy_cache_hit_pct':hit,'proxy_max_kv_usage':max(ks) if ks else None,
                'proxy_preempt_delta':delta('vllm:num_preemptions_total')}

class Sweep:
    def __init__(self,args,tool,config,root):
        self.args=args; self.tool=tool; self.config=config; self.root=root
        self.rows=[]; self.proc=None
        u=urlsplit(args.proxy_url)
        if u.scheme not in {'http','https'} or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {'','/'}:
            raise ValueError('--proxy-url必须为不含路径或凭据的http(s)://host:port')
        self.url=args.proxy_url.rstrip('/')
        self.host=u.hostname; self.port=u.port or (443 if u.scheme=='https' else 80)
        self.metrics=ProxyMetrics(self.url+'/metrics',config.get('API_KEY',''))
        self.scheme=u.scheme
    def make_work(self,cell,mode):
        work=cell/'tool'
        work.mkdir()
        for p in self.tool.glob('*.py'):
            if p.is_file(): shutil.copy2(p,work/p.name)
        shutil.copy2(self.tool/'GSM8K.jsonl',work/'GSM8K.jsonl')
        overrides={'HOST_IP':self.host,'HOST_PORT':str(self.port),'URL':'',
                   'DEFAULT_PERFORMANCE_TEST':mode,'POD_INFO':[],
                   'OUTPUT_DIR':str(cell/'outputs'),'DATASET_PATH':str(cell/'datasets')}
        # The installed API supports URL override, needed for https.
        if self.scheme=='https':
            overrides['URL']=self.url+'/v1/chat/completions'
        with (work/'config.py').open('a',encoding='utf-8') as f:
            f.write('\n# '+STAMP+'；仅修改隔离副本\n')
            for key,value in overrides.items(): f.write(key+' = '+repr(value)+'\n')
        return work
    def terminate(self):
        p=self.proc
        if p is None: return
        try:
            os.killpg(p.pid,signal.SIGTERM)
            try: p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid,signal.SIGKILL)
                p.wait(timeout=5)
        except ProcessLookupError:
            pass
        finally:
            self.proc=None
    def invoke(self,cell,kind,ilen,olen,cc,count,mode,cache):
        work=self.make_work(cell,mode)
        cmd=[sys.executable,'-I','-c',CHILD,str(work),kind,str(ilen),str(olen),str(cc),str(count),str(cache)]
        started=time.monotonic()
        with (cell/'raw.log').open('w',encoding='utf-8') as log:
            self.proc=subprocess.Popen(cmd,cwd=work,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                rc=self.proc.wait(timeout=self.args.timeout)
            except subprocess.TimeoutExpired:
                self.terminate()
                return 'TIMEOUT',time.monotonic()-started
            self.proc=None
        return rc,time.monotonic()-started
    def write_rows(self):
        with (self.root/'all_points.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore')
            writer.writeheader(); writer.writerows(self.rows)
        (self.root/'all_points.json').write_text(json.dumps(self.rows,ensure_ascii=False,indent=2),encoding='utf-8')
    def trial(self,scenario,cc,cache,warmup_error=''):
        kind,ilen,olen,limit=scenario
        mode='default_perf' if kind=='P' else 'stable_stage'
        count=max(self.args.min_requests,cc*(self.args.p_waves if kind=='P' else self.args.d_waves))
        cell=self.root/f'{kind}_{ilen}_{olen}_cc{cc}'
        cell.mkdir()
        print(f'[{kind}] input={ilen} output={olen} cc={cc} requests={count} mode={mode}',flush=True)
        row=dict(time=time.strftime('%F %T'),kind=kind,input_len=ilen,output_len=olen,
                 concurrency=cc,data_num=count,mode=mode,sla_pass=False,selection_eligible=False,
                 cell_dir=str(cell),warning='',reason='')
        run_error=''
        with Monitor(self.metrics) as monitor:
            try:
                rc,elapsed=self.invoke(cell,kind,ilen,olen,cc,count,mode,cache)
            except Exception as e:
                self.terminate()
                rc='EXCEPTION'; run_error=str(e)
        row.update(monitor.result())
        row['cache_evidence']='UNAVAILABLE'
        rate=row['proxy_cache_hit_pct']
        if rate is not None:
            row['cache_evidence']='PROXY_ONLY_NOT_D_PROOF'
        if rc!=0:
            row.update(status='TIMEOUT' if rc=='TIMEOUT' else 'RUN_FAILED',
                       reason=f'执行返回{rc}；{run_error}；详见raw.log',warning=warmup_error)
            self.rows.append(row); self.write_rows()
            print('  '+row['status']+'：停止本场景加压，继续后续场景；服务端可能仍有残留请求，后续结果需谨慎解读。',flush=True)
            return row,True
        try:
            d=parse_report((cell/'raw.log').read_text(encoding='utf-8',errors='replace'),mode,self.args.stable_labels)
            row.update({k:v for k,v in d.items() if k in FIELDS})
            row['scope_success']=d['success']; row['scope_failed']=d['failed']
            row['completion_scope']='total' if d['total_success'] is not None else d['stage']
            reasons=[warmup_error] if warmup_error else []; warnings=[]
            if d['success']<=0 or d['failed']!=0 or (d['total_failed'] is not None and d['total_failed']!=0):
                reasons.append('存在失败请求或目标阶段无成功请求')
            if d['qps']<=0 or not math.isfinite(d['qps']): reasons.append('无效QPS')
            if d['total_success'] is not None and int(d['total_success'])!=count:
                reasons.append('全量成功请求数与计划样本数不一致')
            if kind=='P' and int(d['success'])!=count:
                reasons.append('P全量成功请求数与计划样本数不一致')
            if d['total_success'] is None and kind=='D':
                warnings.append('只验证了稳态阶段请求数，日志未提供全量成功计数')
            length=d.get('output_tokens_mean')
            if length is not None and abs(length-olen)>max(.05,olen*.02):
                reasons.append('实际输出均长偏离目标超过容差（2%，最少0.05 token）')
            if d.get('input_tokens_mean',ilen)<ilen*.98:
                reasons.append('实际输入均长明显小于目标')
            metric='ttft_mean_ms' if kind=='P' else 'tpot_mean_ms'
            if d[metric]<=0 or d[metric] in {9999,99999}:
                reasons.append('目标时延无效或疑似占位值')
            row['sla_pass']=not reasons and d[metric]<=limit
            if kind=='D' and d['success']<2*cc:
                reasons.append('稳态成功样本不足2倍并发；请增加--d-waves')
            if kind=='P' and rate is not None and rate>self.args.p_max_hit:
                reasons.append('P端可观测缓存命中率过高，疑似缓存污染')
            if kind=='D' and rate is not None and rate<self.args.min_hit:
                reasons.append('proxy可观测缓存命中率未达到阈值')
            if rate is None:
                warnings.append('proxy未暴露可用缓存计数；不能证实冷/热缓存条件')
                if kind=='D' and self.args.require_cache_evidence:
                    reasons.append('要求缓存证据但proxy指标不可用')
            if kind=='D': warnings.append('共享前缀+经proxy压测，不能独立证明D端KV完全就绪')
            if (row.get('proxy_preempt_delta') or 0)>0:
                warnings.append('proxy可见抢占发生；仅按用户mean门槛择优，不证明长期稳定')
            if (row.get('proxy_max_kv_usage') or 0)>=.95:
                warnings.append('proxy可见KV使用率>=95%')
            if cc==scan_plan(self.args,scenario)[-1]:
                warnings.append('达到本场景扫描上限，可能仍有提升空间')
            row['status']='INVALID' if reasons else ('PASS' if row['sla_pass'] else 'SLA_FAIL')
            row['selection_eligible']=not reasons and row['sla_pass']
            row['reason']='；'.join(reasons) if reasons else ('' if row['sla_pass'] else f'{metric}超过{limit} ms')
            row['warning']='；'.join(warnings)
            print(f'  {row["status"]} {metric}={d[metric]:.3f} ms QPS={d["qps"]:.6f} stage={d["stage"]} cache={rate}',flush=True)
        except Exception as e:
            row.update(status='PARSE_FAILED',reason=str(e),warning=warmup_error)
            print('  PARSE_FAILED：'+str(e),flush=True)
        self.rows.append(row); self.write_rows()
        # Stop only this scenario on parse errors; later scenarios still get a trial.
        stop=row['status']=='PARSE_FAILED'
        return row,stop

def scan_plan(args, scenario):
    """User reference: 32 cards -> 16 cards; center estimates, not measured limits."""
    if args.scan_profile=='powers2':
        cap=args.max_concurrency or 128
        points=[1]; n=2
        while n<=cap:
            points.append(n); n*=2
        if points[-1]!=cap: points.append(cap)
        return points
    kind,ilen,_,_=scenario
    plans={('P',4096):[20,30,40,50,60],
           ('P',16384):[1,2,3,4,5,6], ('P',65536):[1,2,3,4,5,6],
           ('D',4096):[205,308,410,513,615],
           ('D',16384):[160,240,320,400,480],
           ('D',65536):[160,240,320,400,480]}
    points=plans[kind,ilen]
    cap=args.max_concurrency
    if cap is not None and cap<points[-1]:
        points=[x for x in points if x<=cap]
        if not points or points[-1]!=cap: points.append(cap)
    return points


def best_point(rows,kind,ilen):
    candidates=[r for r in rows if r['kind']==kind and r['input_len']==ilen and r.get('selection_eligible')]
    return max(candidates,key=lambda r:(r['qps'],-r['concurrency'])) if candidates else None

def run_scenarios(sweep):
    """Attempt every selected scenario, even when earlier scenarios fail."""
    args=sweep.args; root=sweep.root; exitcode=0
    for scenario in SCENARIOS:
        kind,ilen,olen,limit=scenario
        if args.only not in {'both',kind}: continue
        coarse=scan_plan(args,scenario)
        cache=root/f'D_shared_prompt_{ilen}.txt'
        warmup_error=''
        if kind=='D':
            for label,num in [('fill',1),('verify',4)]:
                cell=root/f'D_{ilen}_{olen}_warmup_{label}'; cell.mkdir()
                print(f'[D预热] input={ilen} phase={label} mode=default_perf requests={num}',flush=True)
                try:
                    rc,_=sweep.invoke(cell,'D',ilen,1,1,num,'default_perf',cache)
                    if rc!=0: raise ValueError('预热执行失败/超时：'+str(rc))
                    d=parse_report((cell/'raw.log').read_text(encoding='utf-8',errors='replace'),'default_perf',args.stable_labels)
                    if d['success']!=num or d['failed']!=0: raise ValueError('预热请求未全部成功')
                except Exception as e:
                    sweep.terminate()
                    warmup_error='D预热未通过：'+str(e)
                    sweep.rows.append(dict(time=time.strftime('%F %T'),kind=kind,input_len=ilen,output_len=olen,
                        concurrency=1,data_num=num,mode='default_perf',status='WARMUP_FAILED',
                        sla_pass=False,selection_eligible=False,reason=warmup_error,cell_dir=str(cell)))
                    sweep.write_rows(); exitcode=2
                    print('  '+warmup_error+'；仍尝试一次正式压测，结果不参与最佳点选择。',flush=True)
                    break
        tested=[]; misses=0; scenario_failed=False
        for cc in coarse:
            r,stop=sweep.trial(scenario,cc,cache,warmup_error=warmup_error); tested.append(cc)
            if stop:
                scenario_failed=True; exitcode=2; break
            if warmup_error:
                break  # Formal coverage is required, but do not escalate unverified D load.
            if r['status']=='SLA_FAIL': misses+=1
            else: misses=0
            if misses>=2 and best_point(sweep.rows,kind,ilen):
                print('  快速模式：已有达标点且连续两档超过mean门槛，停止更高并发。',flush=True)
                break
            if (r.get('proxy_max_kv_usage') or 0)>=.98 or (r.get('proxy_preempt_delta') or 0)>0:
                print('  proxy可见KV接近满载或发生抢占，停止提高本场景并发。',flush=True)
                break
        if scenario_failed or warmup_error:
            print('  本场景已尝试正式压测，继续下一个场景。',flush=True)
            continue
        b=best_point(sweep.rows,kind,ilen)
        if b and args.refine_points:
            lower=max((x for x in tested if x<b['concurrency']),default=None)
            upper=min((x for x in tested if x>b['concurrency']),default=None)
            mids=[]
            for left,right in [(lower,b['concurrency']),(b['concurrency'],upper)]:
                if left is not None and right is not None and right-left>1:
                    mid=(left+right)//2
                    if mid not in tested and mid not in mids: mids.append(mid)
            for cc in mids[:args.refine_points]:
                r,stop=sweep.trial(scenario,cc,cache)
                if stop:
                    exitcode=2; break
    return exitcode

def summary(root,rows,only):
    selected=[]; lines=[]
    lines += ['PD QPS 快速摸底结果', '='*72, STAMP,
              '仅通过proxy，P为P主导全程统计，D为共享前缀的稳态统计。',
              '最高QPS仅指本次已测且满足条件的点；快速扫描不是全局最优证明。',
              '缓存证据仅来自proxy；缺失时标注未验证，不能证明D侧缓存状态。', '']
    for kind,ilen,olen,limit in SCENARIOS:
        b=best_point(rows,kind,ilen)
        base={'kind':kind,'input_len':ilen,'output_len':olen,
              'sla_metric':'ttft_mean_ms' if kind=='P' else 'tpot_mean_ms','sla_limit_ms':limit}
        if b:
            base.update(b)
            metric=base['sla_metric']
            lines.append(f'{kind} {ilen//1024}K -> {olen} tokens | cc={b["concurrency"]} | '+
                         f'{metric}={b[metric]:.3f} ms | QPS={b["qps"]:.6f} | stage={b["stage"]}')
            if b.get('warning'): lines.append('  注意：'+b['warning'])
        else:
            base.update(status='SKIPPED' if only not in {'both',kind} else 'NO_ELIGIBLE_POINT')
            lines.append(f'{kind} {ilen//1024}K -> {olen} tokens | 未找到符合条件的工作点（或尚未执行）')
        selected.append(base)
    lines += ['', '配平候选（不是已经验证的最佳PD实例比）：']
    ratios=[]
    for ilen in [4096,16384,65536]:
        p=best_point(rows,'P',ilen); d=best_point(rows,'D',ilen)
        if not p or not d:
            lines.append(f'  输入{ilen//1024}K：P或D无可用点，不计算。')
            continue
        ratio=p['qps']/d['qps']
        ratios.append({'input_len':ilen,'output_len':d['output_len'],'p_qps':p['qps'],
                       'd_qps':d['qps'],'d_capacity_units_per_p_capacity_unit':ratio,
                       'scope':'proxy P total / proxy D stable; topology not inferred'})
        lines.append(f'  {ilen//1024}K/{d["output_len"]//1024}K：P_QPS/D_QPS={ratio:.3f}，容量折算约1:{ratio:.3f}。')
    lines += ['  上述换算还要求P/D端各自代表单个、固定规模的实例组。',
              '  若proxy背后已有多P、多D，这是池级容量折算，不能直接当作单实例配比。',
              '  P的default_perf全程QPS与D的stable_stage稳态QPS统计窗口不同，最终需全链路验证。',
              '', '输出目录：'+str(root), '全部点：all_points.csv / all_points.json',
              '最佳点：best_points.csv；容量折算：capacity_ratios.csv',
              '原始日志：各测试目录/raw.log；原工具config.py与日志均不修改。',
              '说明：输入长度是原始文本token数；chat模板开销以input_tokens_mean实际值为准。',
              '共享前缀是合成性能场景；不能代替真实内容分布下的投机接受率和业务负载验证。']
    text='\n'.join(lines)+'\n'
    (root/'summary.txt').write_text(text,encoding='utf-8')
    keys=['kind','input_len','output_len','sla_metric','sla_limit_ms']+[k for k in FIELDS if k not in {'kind','input_len','output_len'}]
    with (root/'best_points.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore'); w.writeheader(); w.writerows(selected)
    with (root/'capacity_ratios.csv').open('w',encoding='utf-8-sig',newline='') as f:
        keys=['input_len','output_len','p_qps','d_qps','d_capacity_units_per_p_capacity_unit','scope']
        w=csv.DictWriter(f,fieldnames=keys); w.writeheader(); w.writerows(ratios)
    print('\n'+text,flush=True)

def self_test():
    # No network, service, ais_bench, tokenizer, or Linux required.
    table='''
| Performance Parameters | Stage | Average | Min | P90 |
| TTFT | total | 0.8 s | 0.1 s | 1.2 s |
| TPOT | total | 25 ms | 1 ms | 30 ms |
| TPOT | stable | 0.019 s | 1 ms | 25 ms |
| OutputTokens | total | 1024 | 1024 | 1024 |
| OutputTokens | stable | 1024 | 1024 | 1024 |
| Common Metric | Stage | Value |
| Total Requests | total | 32 |
| Success Requests | total | 32 |
| Failed Requests | total | 0 |
| Benchmark Duration | total | 40 s |
| Request Throughput | total | 0.8 req/s |
| Success Requests | stable | 16 |
| Failed Requests | stable | 0 |
| Benchmark Duration | stable | 20000 ms |
| Request Throughput | stable | 0.75 req/s |
'''
    labels='stable,stable_stage,steady,steady_stage,稳态'
    p=parse_report(table,'default_perf',labels)
    d=parse_report(table,'stable_stage',labels)
    assert p['ttft_mean_ms']==800 and p['qps']==.8
    assert d['tpot_mean_ms']==19 and d['qps']==.75 and d['success']==16 and d['total_success']==32
    assert parse_report(table.replace('| Request Throughput | stable | 0.75 req/s |',''),'stable_stage',labels)['qps']==.8
    for invalid in [table.replace('stable','warmup'), table.replace('| Failed Requests | stable | 0 |',''), table.replace('0.019 s','19')]:
        try: parse_report(invalid,'stable_stage',labels)
        except ValueError: pass
        else: raise AssertionError('Invalid stable data incorrectly accepted')
    unicode_table=table.replace('|','│')
    assert parse_report(unicode_table,'stable_stage',labels)['qps']==.75
    assert parse_report(table.replace('stable','STEADY_STAGE'),'stable_stage',labels)['stage']=='STEADY_STAGE'
    compile(CHILD,'child','exec')
    sample=[dict(kind='P',input_len=4096,selection_eligible=True,qps=2,concurrency=4),
            dict(kind='P',input_len=4096,selection_eligible=False,qps=99,concurrency=128)]
    assert best_point(sample,'P',4096)['concurrency']==4
    scaled=argparse.Namespace(scan_profile='scaled32',max_concurrency=None)
    expected=[[20,30,40,50,60],[1,2,3,4,5,6],[1,2,3,4,5,6],
              [205,308,410,513,615],[160,240,320,400,480],[160,240,320,400,480]]
    assert [scan_plan(scaled,s) for s in SCENARIOS]==expected
    assert [4*x for x in [40,4,4,410,320,320]]==[160,16,16,1640,1280,1280]
    scaled.max_concurrency=400
    assert scan_plan(scaled,SCENARIOS[3])==[205,308,400]
    with tempfile.TemporaryDirectory() as td:
        root=Path(td); work=root/'benchmark'; backup=root/'backup'; backup.mkdir()
        target=work/'ais_bench/benchmark/configs/models/vllm_api/vllm_api_chat_temp.py'
        target.parent.mkdir(parents=True); target.write_text('original',encoding='utf-8')
        links=SharedLinks(work,backup)
        target.write_text('changed',encoding='utf-8')
        test=work/'ais_bench/datasets/gsm8k/test.jsonl'
        test.write_text('generated',encoding='utf-8')
        links.restore()
        assert target.read_text()=='original' and not test.exists()
    # Exercise the real trial/orchestrator without network, tokenizer, or services.
    import contextlib
    import io
    class OfflineSweep(Sweep):
        def __init__(self,root,failure,only):
            self.root=root; self.failure=failure; self.rows=[]; self.proc=None
            self.metrics=argparse.Namespace(fetch=lambda: {})
            self.args=argparse.Namespace(only=only,scan_profile='powers2',max_concurrency=2,
                refine_points=2,min_requests=0,p_waves=4,d_waves=4,stable_labels=labels,
                p_max_hit=5,min_hit=95,require_cache_evidence=False)
            self.attempts=[]
            if failure=='refine-run':
                self.args.scan_profile='scaled32'; self.args.max_concurrency=None
        def invoke(self,cell,kind,ilen,olen,cc,count,mode,cache):
            warmup='_warmup_' in cell.name
            if not warmup: self.attempts.append((kind,ilen,cc))
            failure=self.failure
            if warmup:
                active=failure.startswith('warmup-')
                if failure=='warmup-verify':
                    active=cell.name.endswith('_verify')
                if active:
                    if failure=='warmup-exception': raise OSError('offline warmup error')
                    if failure in {'warmup-run','warmup-verify'}: return 1,0
                    if failure=='warmup-timeout': return 'TIMEOUT',0
            else:
                if failure=='run': return 1,0
                if failure=='timeout': return 'TIMEOUT',0
                if failure=='exception': raise OSError('offline launch error')
                if failure=='interrupt': raise KeyboardInterrupt()
                if failure=='refine-run' and cc not in scan_plan(self.args,(kind,ilen,olen,0)):
                    return 1,0
            report=table.replace('| total | 32 |',f'| total | {count} |')
            report=report.replace('| stable | 16 |',f'| stable | {count} |')
            report=report.replace('| 1024 | 1024 | 1024 |',f'| {olen} | {olen} | {olen} |')
            if not warmup:
                if failure=='sla': report=report.replace('0.8 s','9 s').replace('0.019 s','0.021 s')
                if failure=='invalid': report=report.replace(f'| {olen} | {olen} | {olen} |','| 99999 | 99999 | 99999 |')
                if failure=='refine-run':
                    center=scan_plan(self.args,(kind,ilen,olen,0))[1]
                    qps=1000/(1+abs(cc-center))
                    report=report.replace('0.8 req/s',f'{qps} req/s').replace('0.75 req/s',f'{qps} req/s')
            if (warmup and failure=='warmup-parse') or (not warmup and failure=='parse'):
                report='unparseable offline report'
            if warmup and failure=='warmup-count':
                report=report.replace(f'| Success Requests | total | {count} |','| Success Requests | total | 0 |')
            (cell/'raw.log').write_text(report,encoding='utf-8')
            return 0,0
    for failure in ['none','sla','invalid','parse','run','timeout','exception','refine-run',
                    'warmup-run','warmup-timeout','warmup-parse','warmup-count','warmup-exception','warmup-verify']:
        for only in ['both','P','D']:
            with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
                sweep=OfflineSweep(Path(td),failure,only)
                rc=run_scenarios(sweep)
                expected={(k,i) for k,i,_,_ in SCENARIOS if only in {'both',k}}
                assert {(k,i) for k,i,_ in sweep.attempts}==expected, (failure,only,sweep.attempts)
                formal=[r for r in sweep.rows if r['status']!='WARMUP_FAILED']
                assert {(r['kind'],r['input_len']) for r in formal}==expected
                if failure.startswith('warmup-') and only!='P':
                    drows=[r for r in formal if r['kind']=='D']
                    assert len(drows)==3 and all(r['status']=='INVALID' and not r['selection_eligible'] for r in drows)
                    assert all('D预热未通过' in r['reason'] for r in drows)
                    assert rc==2
                elif failure in {'parse','run','timeout','exception'}:
                    assert len(formal)==len(expected) and rc==2
                elif failure=='refine-run':
                    assert rc==2  # A failed refinement must not skip later scenarios either.
                else:
                    assert rc==0
                if failure in {'sla','invalid'}:
                    assert all(not r['selection_eligible'] for r in formal)
    with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
        try: run_scenarios(OfflineSweep(Path(td),'interrupt','both'))
        except KeyboardInterrupt: pass
        else: raise AssertionError('User interruption must still stop the scan')
    print('SELF-TEST PASSED: stage isolation, no-total-fallback, missing metric rejection, units, Unicode, best-point selection, shared-file restore, child syntax; 42 offline scenario-coverage cases (SLA/invalid/run/timeout/parse/launch/refinement/warmup failures, --only filters), user interruption')

def main():
    ap=argparse.ArgumentParser(description='经proxy扫描纯P/共享前缀纯D的达标QPS；原配置不修改。',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument('--tool-dir',default='.',help='包含aisbench_test.py和config.py的工具目录')
    ap.add_argument('--proxy-url',default='http://127.0.0.1:1999',help='proxy根地址')
    ap.add_argument('--only',choices=['both','P','D'],default='both')
    ap.add_argument('--scan-profile',choices=['scaled32','powers2'],default='scaled32',help='scaled32按用户32卡参考折半设扫描点；powers2从1倍增')
    ap.add_argument('--max-concurrency',type=int,default=None,help='可选的统一并发硬上限；默认使用各场景扫描上限')
    ap.add_argument('--p-waves',type=int,default=4,help='P请求数为并发的此倍数')
    ap.add_argument('--d-waves',type=int,default=4,help='D请求数为并发的此倍数')
    ap.add_argument('--min-requests',type=int,default=0,help='可选样本下限；默认0确保每档请求数恰为4倍并发')
    ap.add_argument('--refine-points',type=int,default=2,choices=[0,1,2],help='粗扫后额外测试最佳点两侧中点')
    ap.add_argument('--timeout',type=int,default=1200,help='每次执行超时秒数，包含数据准备及模型分词器加载')
    ap.add_argument('--min-hit',type=float,default=95,help='D正式请求可观测缓存命中率下限，百分数')
    ap.add_argument('--p-max-hit',type=float,default=5,help='P可观测缓存命中率上限，百分数')
    ap.add_argument('--require-cache-evidence',action='store_true',help='proxy无缓存指标时不允许选择D最佳点')
    ap.add_argument('--stable-labels',default='stable,stable_stage,stable stage,steady,steady_stage,steady state,稳态',help='允许的稳态Stage标签，逗号分隔；不允许total')
    ap.add_argument('--out-dir',help='新建结果目录，必须尚不存在')
    ap.add_argument('--self-test',action='store_true',help='只运行离线自测，不访问服务')
    args=ap.parse_args()
    if args.self_test:
        self_test(); return 0
    if fcntl is None:
        raise ValueError('真实压测必须在Linux评测容器运行')
    if min(args.p_waves,args.d_waves,args.timeout)<1 or args.min_requests<0 or (args.max_concurrency is not None and args.max_concurrency<1):
        raise ValueError('并发上限、样本倍数、超时必须为正数；最少请求数不能为负数')
    labels={norm(s) for s in args.stable_labels.split(',')}
    if labels & {'total','all','overall','nostage','warmup','cooldown'}:
        raise ValueError('--stable-labels禁止包含全程、缺失阶段或非稳态标签')
    if not 0<=args.min_hit<=100 or not 0<=args.p_max_hit<=100:
        raise ValueError('缓存百分数必须在0~100之间')
    tool=Path(args.tool_dir).resolve()
    for name in ['config.py','aisbench_test.py','GSM8K.jsonl','default_api.py']:
        if not (tool/name).is_file(): raise ValueError('工具目录缺少：'+str(tool/name))
    config=read_config(tool/'config.py')
    work=Path(config['WORK_PATH']).resolve()
    if not work.is_dir(): raise ValueError('WORK_PATH不存在：'+str(work))
    if shutil.which('ais_bench') is None: raise ValueError('当前容器找不到ais_bench命令')
    if args.only!='P':
        stage=work/'ais_bench/benchmark/configs/summarizers/perf/stable_stage.py'
        if not stage.is_file(): raise ValueError('当前WORK_PATH没有stable_stage配置：'+str(stage))
    root=Path(args.out_dir).resolve() if args.out_dir else tool/'pd_qps_results'/(time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
    root.mkdir(parents=True,exist_ok=False,mode=0o700)
    backup=root/'shared_link_backup'; backup.mkdir()
    sweep=Sweep(args,tool,config,root)
    print('结果目录：'+str(root),flush=True)
    print('只访问proxy='+sweep.url+'；不会修改原config.py、重启服务或清全局缓存。',flush=True)
    print('D先以default_perf尝试两轮共享前缀预热，再以stable_stage正式测试。',flush=True)
    print('每个选中场景至少尝试一次正式压测；单场景失败不阻断后续场景。',flush=True)
    print('D预热失败仍试跑一次，但不参与择优；超时/执行失败后服务端可能仍有残留请求。',flush=True)
    print('参考并发：32卡P=80/8/8、D=819/640/640；16卡估计P=40/4/4、D=410/320/320。',flush=True)
    print('扫描计划（正式请求数默认=并发×4；预热另计）：',flush=True)
    for s in SCENARIOS:
        if args.only in {'both',s[0]}:
            print(f'  {s[0]} {s[1]//1024}K/{s[2]}: '+str(scan_plan(args,s)),flush=True)
    print('proxy若无/metrics，缓存条件将标为未验证；可加--require-cache-evidence禁止择优。',flush=True)
    manifest={'version':VERSION,'tool_dir':str(tool),'proxy_url':sweep.url,'model_name':config['MODEL_NAME'],
              'options':vars(args),'scenarios':SCENARIOS,
              'scan_plans':{f'{s[0]}_{s[1]}':scan_plan(args,s) for s in SCENARIOS},'note':STAMP}
    (root/'run_config.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    locks=[]; links=None; exitcode=0
    def interrupted(signum,frame):
        raise KeyboardInterrupt('signal='+str(signum))
    old_handlers={s:signal.signal(s,interrupted) for s in [signal.SIGINT,signal.SIGTERM]}
    try:
        for path in [tool/'.pd_qps_sweep.lock',work/'.pd_qps_sweep.lock']:
            f=path.open('a+')
            try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                f.close(); raise RuntimeError('另一个扫描占用工具/WORK_PATH；请勿同时启动其他aisbench测试')
            locks.append(f)
        links=SharedLinks(work,backup)
        recovery=[{'path':str(p),'kind':kind,'saved':str(saved) if saved is not None else None} for p,(kind,saved) in links.entries]
        (backup/'manifest.json').write_text(json.dumps(recovery,ensure_ascii=False,indent=2),encoding='utf-8')
        exitcode=run_scenarios(sweep)
        sweep.write_rows()
    except KeyboardInterrupt:
        print('\n收到中断，终止本脚本的压测子进程并恢复共享链接。',flush=True)
        exitcode=130
    except Exception as e:
        print('ERROR：'+str(e),file=sys.stderr,flush=True)
        exitcode=2
    finally:
        try:
            sweep.terminate()
        finally:
            if links is not None:
                try: links.restore()
                except Exception as e:
                    print('ERROR：'+str(e),file=sys.stderr); exitcode=2
            for f in locks: f.close()
            for s,old in old_handlers.items(): signal.signal(s,old)
            summary(root,sweep.rows,args.only)
    if exitcode==0:
        wanted=[s for s in SCENARIOS if args.only in {'both',s[0]}]
        if any(best_point(sweep.rows,s[0],s[1]) is None for s in wanted): exitcode=3
    return exitcode

if __name__=='__main__':
    try:
        sys.exit(main())
    except (ValueError,OSError) as e:
        print('ERROR：'+str(e),file=sys.stderr)
        sys.exit(2)
PD_QPS_PYTHON
