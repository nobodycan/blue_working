"""Create an exact token-ID workload. This synthetic workload is not an accuracy set."""
import argparse
import hashlib
import json
from pathlib import Path
import random


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--model', required=True)
    p.add_argument('--length', type=int, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    from vllm.tokenizers import get_tokenizer
    tok = get_tokenizer(a.model, tokenizer_mode='deepseek_v4', trust_remote_code=True)
    text = ('Analyze data structures and explain system performance with clear examples. '
            'The server receives a request and processes the context before generating output. '
            'Compute memory capacity latency bandwidth queue scheduler token sequence attention. '
            'def example(value): return value * 2\n'
            '0123456789 abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ ')
    special = set(getattr(tok, 'all_special_ids', []) or [])
    pool = sorted(set(tok.encode(text, add_special_tokens=False))-special)
    if len(pool) < 16: raise RuntimeError('Tokenizer produced too few ordinary token IDs')
    ids = random.Random(20261021+a.length).choices(pool, k=a.length)
    data = dict(length=a.length, prompt_token_ids=ids, pool=pool, seed=20261021,
                scheme='random ordinary token IDs, unique 32-token header per request')
    payload = json.dumps(data, separators=(',', ':'))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(payload, encoding='utf-8')
    print(json.dumps(dict(file=str(a.out), length=len(ids),
                         sha256=hashlib.sha256(payload.encode()).hexdigest())))


if __name__ == '__main__': main()
