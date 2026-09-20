"""Observation only: no changes to model messages, results, or completion policy."""
import dataclasses
import hashlib
import json
import threading
import time
from pathlib import Path


def clean(value):
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        if value.get('type') in ('reasoning', 'thinking'):
            return {'type': value['type'], 'omitted': True}
        return {k: clean(v) for k, v in value.items()
                if k not in ('reasoning_content', '_nz_provider_reasoning_content',
                             'provider_extra', 'private_reasoning')
                and not (k in ('reasoning', 'thinking') and isinstance(v, str))}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    return value


def append(path, value):
    with Path(path).open('a', encoding='utf-8') as f:
        f.write(json.dumps(clean(value), ensure_ascii=False, default=str) + '\n')


def save(path, value):
    Path(path).write_text(json.dumps(clean(value), ensure_ascii=False, default=str, indent=2) + '\n')


def hashes(workspace):
    excluded = {'.agent', '.nz-coder', '.git', '__pycache__', '.pytest_cache'}
    return {str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(workspace).rglob('*'))
            if p.is_file() and not excluded.intersection(p.relative_to(workspace).parts)}


class Capture:
    def __init__(self, out, workspace):
        self.out, self.workspace = Path(out), Path(workspace)
        self.env = None
        self.purpose = None
        self.request_id = 0
        self.main = 0
        self.auxiliary = 0
        self.sequence = 0
        self.version = -1
        self.last_hashes = None
        self.lock = threading.RLock()

    def snapshot(self, reason, **extra):
        with self.lock:
            self.sequence += 1
            current = hashes(self.workspace)
            if current != self.last_hashes:
                self.version += 1
                self.last_hashes = current
                target = self.out / 'workspace-versions' / f'v{self.version:03d}'
                target.mkdir(parents=True)
                for name in current:
                    dest = target / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes((self.workspace / name).read_bytes())
                save(target / 'hashes.json', current)
            env = self.env
            record = {'seq': self.sequence, 'time': time.time(), 'reason': reason,
                      'request_id': self.request_id, 'purpose': self.purpose,
                      'main_requests': self.main, 'auxiliary_requests': self.auxiliary,
                      'workspace': str(self.workspace), 'workspace_version': self.version,
                      'workspace_hashes': current, **extra}
            if env is not None:
                record.update(run_id=env.tracer.run_id, state=env.runtime_state.to_dict(),
                              run_evidence=env.run_evidence.to_dict(), verification_manager=env.vm.status())
            append(self.out / 'state-snapshots.jsonl', record)
            return self.sequence

    def request_hook(self, request):
        with self.lock:
            # Set by the actual Gateway event, never inferred from model tool arguments.
            if self.purpose is None:
                raise RuntimeError('No observed Gateway purpose; refusing unattributed paid request')
            self.request_id += 1
            if self.purpose == 'coding':
                if self.main >= 12:
                    raise RuntimeError('12 actual main request cap reached')
                self.main += 1
            else:
                self.auxiliary += 1
            seq = self.snapshot('before_provider_http_request')
            request.headers['x-nz-capture-id'] = str(self.request_id)
            request.headers['x-nz-capture-purpose'] = self.purpose
            request.headers['x-nz-capture-snapshot'] = str(seq)

    def install(self, env):
        self.env = env
        original_log = env.tracer.log

        def observed_log(event, **payload):
            if event.startswith('model_call_') and payload.get('purpose'):
                self.purpose = payload['purpose']
            original_log(event, **payload)
            append(self.out / 'runtime-full.jsonl', {
                'time': time.time(), 'run_id': env.tracer.run_id,
                'request_id': self.request_id, 'event': event, **payload})
            if (event.startswith('model_call_') or event in
                    ('terminal_boundary_settled', 'verification_result', 'run_end') or
                    'semantic' in event or 'sidecar' in event):
                self.snapshot('trace:' + event)

        env.tracer.log = observed_log
        original_record = env._record_tool_result

        def observed_record(result):
            before = self.snapshot('before_record_tool_result', tool=result.name)
            returned = original_record(result)
            after = self.snapshot('after_record_tool_result', tool=result.name)
            append(self.out / 'tool-results.jsonl', {
                'request_id': self.request_id, 'before_snapshot': before,
                'after_snapshot': after, 'result': dataclasses.asdict(result)})
            return returned

        env._record_tool_result = observed_record
        self.snapshot('environment_built')
