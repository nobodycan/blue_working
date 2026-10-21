"""Make a separate instrumented copy of the upstream example. Never overwrite upstream."""
import argparse
import ast
from pathlib import Path

INSTRUMENTATION = '''
# ASCEND_BENCH_PROXY_AUDIT_V1
import contextvars as _audit_contextvars
from capacity_probe import write_record as _audit_write
_audit_rid = _audit_contextvars.ContextVar('bench_request_id', default=None)
_audit_original_next = next_req_id

def next_req_id():
    return _audit_rid.get() or _audit_original_next()

@app.middleware("http")
async def _audit_request_id_middleware(request, call_next):
    rid = request.headers.get("x-request-id")
    token = _audit_rid.set(rid)
    try:
        return await call_next(request)
    finally:
        _audit_rid.reset(token)

_audit_original_send = send_request_to_service
async def send_request_to_service(client, endpoint, req_data, request_id, **kwargs):
    start = time.perf_counter_ns()
    try:
        return await _audit_original_send(client, endpoint, req_data, request_id, **kwargs)
    finally:
        _audit_write(os.getenv("PROXY_AUDIT_DIR"), "prefill_http_roundtrip", {
            "request_id":request_id, "duration_ms":(time.perf_counter_ns()-start)/1e6,
            "backend":str(client.base_url)})

_audit_original_stream = stream_service_response_with_retry
async def stream_service_response_with_retry(client, endpoint, req_data, request_id, **kwargs):
    start = time.perf_counter_ns()
    first = True
    async for chunk in _audit_original_stream(client, endpoint, req_data, request_id, **kwargs):
        if first and chunk:
            first = False
            _audit_write(os.getenv("PROXY_AUDIT_DIR"), "decode_http_first_bytes", {
                "request_id":request_id, "duration_ms":(time.perf_counter_ns()-start)/1e6,
                "backend":str(client.base_url)})
        yield chunk

'''


def main():
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path); p.add_argument('output', type=Path)
    a = p.parse_args()
    source = a.source.read_text(encoding='utf-8')
    for anchor in ('def next_req_id(', 'async def send_request_to_service(',
                   'async def stream_service_response_with_retry(', '@app.post("/v1/completions")'):
        if anchor not in source: raise RuntimeError(f'Upstream mismatch: {anchor}')
    anchor = 'if __name__ == "__main__":'
    if source.count(anchor) != 1: raise RuntimeError('Unexpected main entry point')
    modified = source.replace(anchor, INSTRUMENTATION+'\n'+anchor)
    ast.parse(modified)
    a.output.write_text(modified, encoding='utf-8')
    print(a.output)


if __name__ == '__main__': main()
