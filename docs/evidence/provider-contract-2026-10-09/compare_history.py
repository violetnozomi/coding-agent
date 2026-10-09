"""离线核对既有40个B/N请求，不打印提示、工具正文或reasoning。"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter  # noqa: E402
from nz_coder.evaluation.model_relay import _Usage  # noqa: E402


def main():
    counter = DeepSeekV41Counter(Path(sys.argv[1]))
    output = Path(sys.argv[2])
    rows = []
    for task in ('B', 'N'):
        for side in ('nzcoder', 'infcodex'):
            source = ROOT / 'docs/evidence/paid-comparison-2026-09-16' / task / side
            requests = [json.loads(line) for line in (source/'provider-requests.jsonl').read_text().splitlines()]
            responses = [json.loads(line) for line in (source/'provider-responses.jsonl').read_text().splitlines()]
            assert len(requests) == len(responses)
            for index, (request, response) in enumerate(zip(requests, responses), 1):
                payload = request['payload']
                raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
                counted = counter(raw, payload)
                usage = _Usage(bool(payload.get('stream')))
                usage.feed(response['body'].encode())
                reported = usage.finish()
                prompt = reported.get('prompt_tokens') if isinstance(reported, dict) and not usage.invalid else None
                rows.append({'task':task, 'side':side, 'request':index, 'time':request['time'],
                    'request_model':payload['model'], 'response_model':usage.provider_model,
                    'response_fingerprint':usage.system_fingerprint, 'http_status':response['status'],
                    'source_request_sha256':hashlib.sha256((source/'provider-requests.jsonl').read_bytes()).hexdigest(),
                    'source_response_sha256':hashlib.sha256((source/'provider-responses.jsonl').read_bytes()).hexdigest(),
                    'analysis_payload_sha256':counted.payload_sha256, 'payload_original_wire_bytes_available':False,
                    'method':counted.method, 'evidence_level':counted.evidence_level,
                    'reference_count':counted.reference_count, 'reported_prompt_tokens':prompt,
                    'delta_reference_minus_reported':None if prompt is None or counted.reference_count is None else counted.reference_count-prompt,
                    'comparability':'qualified_reference_only: observable model alias matches; historical hosted template/revision not attested'
                        if counted.reference_count is not None and usage.provider_model=='deepseek-flash' else 'unsupported_request_shape_or_identity',
                    'stream':payload.get('stream'), 'thinking':payload.get('thinking'), 'reasoning_effort':payload.get('reasoning_effort'),
                    'max_tokens':payload.get('max_tokens'), 'max_completion_tokens':payload.get('max_completion_tokens'),
                    'has_tool_schema':bool(payload.get('tools')), 'has_tool_history':any(m.get('role')=='tool' for m in payload['messages']),
                    'usage':reported, 'usage_conflict':usage.invalid, 'finish_reason':usage.finish_reason})
    assert len(rows)==40
    output.write_text(json.dumps({'counter':counter.status(), 'physical_historical_requests':len(rows),
                                 'new_remote_requests':0, 'rows':rows},ensure_ascii=False,indent=2)+'\n')
    comparable = [r for r in rows if r['reference_count'] is not None]
    print(json.dumps({'historical_requests':len(rows), 'reference_count_available':len(comparable),
                      'deltas':[r['delta_reference_minus_reported'] for r in comparable], 'new_remote_requests':0}))


if __name__=='__main__':
    main()
