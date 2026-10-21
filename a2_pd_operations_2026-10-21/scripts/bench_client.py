"""Closed-loop sustained load or a finite wave, with exact input/output token checks.

TTFT-token measures first delivered output token ID. TTFT-text measures first
nonempty text delta. Streaming event gaps are never labelled individual-token ITL.
"""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import time
import uuid


def percentile(values, q):
    values = sorted(values)
    if not values: return None
    idx = (len(values)-1)*q
    lo, hi = math.floor(idx), math.ceil(idx)
    return values[lo]+(values[hi]-values[lo])*(idx-lo)


def stats(values):
    if not values: return dict(mean=None, p50=None, p95=None, p99=None)
    return dict(mean=statistics.mean(values), p50=percentile(values, .5),
                p95=percentile(values, .95), p99=percentile(values, .99))


async def main(a):
    import aiohttp
    data = json.loads(a.prompts.read_text(encoding='utf-8'))
    length = data['length']; base = data['prompt_token_ids']; pool = data['pool']
    assert len(base) == length and length >= 32
    a.out.parent.mkdir(exist_ok=True, parents=True)
    raw = a.out.with_suffix('.requests.jsonl').open('w', encoding='utf-8')
    run_id = uuid.uuid4().hex[:12]
    epoch = time.perf_counter()
    start_wall_ns = time.time_ns()
    measure_start = a.burnin if a.mode == 'steady' else 0
    measure_end = a.burnin+a.duration if a.mode == 'steady' else float('inf')
    records = []; issued = 0
    timeout = aiohttp.ClientTimeout(total=a.timeout)
    connector = aiohttp.TCPConnector(limit=a.concurrency, limit_per_host=a.concurrency)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, read_bufsize=8*1024*1024,
                                    trust_env=False) as session:
        async def request_one(index):
            rid = f'bench-{run_id}-{index}'
            ids = list(base)
            seed = int.from_bytes(hashlib.sha256(rid.encode()).digest()[:8], 'big')
            ids[:32] = random.Random(seed).choices(pool, k=32)
            body = dict(model=a.model, prompt=ids, add_special_tokens=False,
                        max_tokens=a.output_length, min_tokens=a.output_length,
                        ignore_eos=True, temperature=0, stream=True,
                        stream_options={'include_usage':True},
                        return_token_ids=a.return_token_ids, request_id=rid)
            encoded = json.dumps(body, separators=(',', ':')).encode()
            t0 = time.perf_counter()-epoch
            first, first_text, last = None, None, None
            events, usage, observed = [], None, 0
            error, finish, done = None, None, False
            try:
                async with session.post(a.base_url.rstrip('/')+'/v1/completions',
                        data=encoded, headers={'Content-Type':'application/json',
                                             'X-Request-Id':rid}) as response:
                    if response.status != 200:
                        raise RuntimeError(f'HTTP {response.status}: {(await response.text())[:1000]}')
                    async for line in response.content:
                        if not line.startswith(b'data:'): continue
                        value = line[len(b'data:'):].strip()
                        if value == b'[DONE]': done=True; break
                        message = json.loads(value)
                        if message.get('error'): raise RuntimeError(str(message['error']))
                        now = time.perf_counter()-epoch
                        if message.get('usage'): usage=message['usage']
                        for choice in message.get('choices', []):
                            token_ids = choice.get('token_ids') or []
                            text = choice.get('text') or ''
                            if token_ids or text:
                                if first is None: first=now
                                last=now
                                events.append({'t_s':now, 'tokens':len(token_ids)})
                            observed+=len(token_ids)
                            if text and first_text is None: first_text=now
                            if choice.get('finish_reason'): finish=choice['finish_reason']
                            if choice.get('stop_reason') == 'recomputed':
                                raise RuntimeError('KV recomputation invalidates the baseline')
                if not done: raise RuntimeError('Missing SSE [DONE] marker')
                if usage is None: raise RuntimeError('Missing usage; cannot validate exact lengths')
                if usage.get('prompt_tokens') != length:
                    raise RuntimeError(f'Input length mismatch: {usage}')
                if usage.get('completion_tokens') != a.output_length or (a.return_token_ids and observed != a.output_length):
                    raise RuntimeError(f'Output mismatch: usage={usage}, observed_token_ids={observed}')
                if first is None or last is None: raise RuntimeError('No output payload received')
            except Exception as exc:
                error = repr(exc)
            end = time.perf_counter()-epoch
            count = (usage or {}).get('completion_tokens', 0)
            row = dict(request_id=rid, start_s=t0, end_s=end, error=error, usage=usage,
                       output_ids=observed, finish_reason=finish, token_events=events,
                       ttft_token_ms=None if not a.return_token_ids or first is None else (first-t0)*1000,
                       ttft_text_ms=None if first_text is None else (first_text-t0)*1000,
                       tpot_request_ms=None if first is None or count<2 else (end-first)*1000/(count-1),
                       tpot_delivery_ms=None if first is None or count<2 else (last-first)*1000/(count-1),
                       e2e_ms=(end-t0)*1000,
                       first_event_tokens=events[0]['tokens'] if events else 0,
                       in_measure_window=t0>=measure_start and end<=measure_end)
            raw.write(json.dumps(row)+'\n'); raw.flush()
            records.append(row)
            if error: print(f'{rid}: {error}', flush=True)

        async def worker():
            nonlocal issued
            while True:
                if a.mode == 'wave':
                    if issued >= a.requests: return
                elif time.perf_counter()-epoch >= measure_end:
                    return
                index=issued; issued+=1
                await request_one(index)

        await asyncio.gather(*(worker() for _ in range(a.concurrency)))
    raw.close()
    elapsed = time.perf_counter()-epoch
    window_end = elapsed if a.mode == 'wave' else measure_end
    included = [r for r in records if r['in_measure_window']]
    valid = [r for r in included if not r['error']]
    failures = [r for r in included if r['error']]
    start_cohort = [r for r in records if measure_start<=r['start_s']<window_end]
    censored = [r for r in start_cohort if r['end_s']>window_end]
    delivered = sum(e['tokens'] for r in records for e in r['token_events']
                    if measure_start<=e['t_s']<window_end)
    summary = dict(run_id=run_id, mode=a.mode, base_url=a.base_url,
        input_tokens=length, output_tokens=a.output_length, client_concurrency=a.concurrency,
        start_wall_ns=start_wall_ns, window_start_s=measure_start, window_end_s=window_end,
        wall_elapsed_s=elapsed, measured_requests=len(included), valid_requests=len(valid),
        measured_failures=len(failures), all_phase_failures=sum(bool(r['error']) for r in records),
        start_window_requests=len(start_cohort), end_censored_requests=len(censored),
        end_censored_fraction=len(censored)/len(start_cohort) if start_cohort else None,
        start_cohort_tpot_ms=stats([r['tpot_request_ms'] for r in start_cohort
                                    if not r['error'] and r['tpot_request_ms'] is not None]),
        sufficient_samples=len(valid)>=a.min_samples,
        return_token_ids=a.return_token_ids,
        delivered_token_id_throughput=delivered/(window_end-measure_start) if a.return_token_ids else None,
        completed_output_token_throughput=sum((r['usage'] or {}).get('completion_tokens',0)
                for r in records if measure_start<=r['end_s']<window_end and not r['error'])/(window_end-measure_start),
        request_completed_throughput=sum(measure_start<=r['end_s']<window_end and not r['error']
                                         for r in records)/(window_end-measure_start),
        **{key:stats([r[key] for r in valid if r[key] is not None]) for key in
           ('ttft_token_ms','ttft_text_ms','tpot_request_ms','tpot_delivery_ms','e2e_ms')})
    a.out.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0 if not failures and not summary['all_phase_failures'] and summary['sufficient_samples'] else 2


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base-url', required=True); p.add_argument('--model', default='dsv4')
    p.add_argument('--prompts', type=Path, required=True); p.add_argument('--out', type=Path, required=True)
    p.add_argument('--concurrency', type=int, default=1)
    p.add_argument('--mode', choices=['steady','wave'], default='steady')
    p.add_argument('--requests', type=int, default=1)
    p.add_argument('--burnin', type=float, default=120); p.add_argument('--duration', type=float, default=600)
    p.add_argument('--timeout', type=float, default=1800)
    p.add_argument('--output-length', type=int, default=1024)
    p.add_argument('--return-token-ids', action='store_true', help='Length-calibration ONLY; echoes prompt IDs in the response')
    p.add_argument('--min-samples', type=int, default=30)
    args=p.parse_args()
    if args.concurrency<1 or args.requests<1 or args.duration<=0 or args.burnin<0:
        p.error('Invalid concurrency, requests, duration or burnin')
    raise SystemExit(asyncio.run(main(args)))
