"""Offline Q-only observations from captured facts; no Provider calls."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
P = OUT / 'Q/nzcoder'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


tools = [json.loads(line) for line in (P / 'tool-results.jsonl').read_text().splitlines()]
timeouts = [x for x in tools if x['result'].get('metadata', {}).get('timed_out')]
reviews = [x for x in tools if x['result']['name'] == 'review_run_evidence']
assert not timeouts and not reviews
save(P / 'timeout-recovery.json', {'observed': False})
save(P / 'review-run-evidence.json', {
    'observed': False, 'calls': [], 'invalid_argument_attempts': None,
    'same_generation_duplicates': None, 'new_evidence_reviews': None,
    'interpretation': 'No calls. Interface behavior remained unobserved; zero calls do not prove removal of guessing.',
})
comparison = {
    'baseline_A': {'main': 16, 'auxiliary': 1, 'acceptance': '7/7', 'terminal': 'completed',
                   'review_calls': 3, 'review_dispatch_failures': 0,
                   'note': 'Three internal evidence-field trials, not three outer dispatch failures'},
    'baseline_B': {'main': 7, 'auxiliary': 0, 'acceptance': '6/7', 'terminal': 'error',
                   'timeout': True, 'next_request_after_timeout': False, 'review_calls': 0},
    'current': {'main': 12, 'auxiliary': 1, 'acceptance': '7/7', 'terminal': 'completed',
                'timeout': False, 'review_calls': 0, 'classification': 'Q-C',
                'permission_denials': sum(bool(x['result'].get('permission_denied')) for x in tools)},
    'comparison': 'Q-only longitudinal historical comparison; no InfCodeX or other task rerun',
}
save(OUT / 'analysis/q-longitudinal-comparison.json', comparison)
save(OUT / 'analysis/timeout-before-after.json', {
    'before': comparison['baseline_B'], 'after': {'observed': False},
    'conclusion': 'No timeout this sample; real-model recovery hypothesis not directly exercised.',
})
save(OUT / 'analysis/review-before-after.json', {
    'before': comparison['baseline_A'], 'after': {'observed': False},
    'conclusion': 'Semantic verifier ran, but review_run_evidence did not; tool-interface efficiency not observed.',
})
save(OUT / 'analysis/causal-findings.json', {
    'strong': ['Independent 7/7 and public 25/25 pass', 'README read at main 2 and changed at main 6',
               'Exact declared Node test executed by Runtime at final boundary, G5/VG5',
               'Natural semantic accept and completed; no post-review mutation'],
    'moderate': ['Current trajectory differs from historical Q-B; stochastic trajectory does not isolate the stream fix'],
    'insufficient': ['No real timeout recovery observation', 'No review_run_evidence interface observation',
                     'No general success-rate or efficiency conclusion'],
    'new_deterministic_core_bug': False,
    'production_modified': False,
})
