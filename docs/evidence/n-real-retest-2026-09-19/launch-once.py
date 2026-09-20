"""Authorized single NZ N. Transport capture only; no InfCodeX entry point."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer

import httpx
from capture import append, clean, hashes, save

ROOT = Path('/home/pyh/nzcoder')
OUT = Path(__file__).resolve().parent
WORK = Path('/tmp/nz-n-real-0885c87-20260919')
OLD = ROOT / 'docs/evidence/paid-comparison-2026-09-16'
NODE = Path('/home/pyh/.local/lib/python3.13/site-packages/playwright/driver/node')


def response_projection(raw):
    text = raw.decode('utf-8', 'replace')
    if text.lstrip().startswith('{'):
        return {'json': clean(json.loads(text))}
    chunks = []
    for line in text.splitlines():
        if line.startswith('data: ') and line[6:] != '[DONE]':
            chunks.append(clean(json.loads(line[6:])))
    return {'chunks': chunks, 'done': 'data: [DONE]' in text}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/v1/chat/completions':
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers['content-length']))
        payload = json.loads(raw)
        purpose = self.headers.get('x-nz-capture-purpose')
        request_id = self.headers.get('x-nz-capture-id')
        with self.server.lock:
            if not request_id or not purpose:
                self.send_error(400, 'Missing capture attribution')
                return
            if purpose == 'coding':
                if self.server.main >= 12:
                    self.send_error(429, '12 main request cap')
                    return
                self.server.main += 1
                ordinal = self.server.main
            else:
                self.server.aux += 1
                ordinal = self.server.aux
            record = {'request_id': int(request_id), 'time': time.time(), 'purpose': purpose,
                      'ordinal': ordinal, 'main_budget_before': 13 - self.server.main if purpose == 'coding' else 12 - self.server.main,
                      'main_count': self.server.main, 'auxiliary_count': self.server.aux,
                      'snapshot_seq': int(self.headers['x-nz-capture-snapshot']), 'payload': clean(payload)}
            append(OUT / 'provider-requests.jsonl', record)
        response_record = {'request_id': int(request_id), 'purpose': purpose}
        try:
            key, base = self.server.credentials
            with httpx.Client(trust_env=False, timeout=httpx.Timeout(180.0, connect=30.0)) as client:
                with client.stream('POST', base + '/v1/chat/completions', content=raw,
                                   headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                            'Accept': self.headers.get('accept', '*/*')}) as response:
                    response_record.update(status=response.status_code,
                                           provider_request_id=response.headers.get('x-request-id'))
                    self.send_response(response.status_code)
                    for k, v in response.headers.items():
                        if k.lower() in ('content-type', 'cache-control', 'x-request-id'):
                            self.send_header(k, v)
                    self.end_headers()
                    chunks = []
                    downstream_open = True
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        if downstream_open:
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                downstream_open = False
                    response_record['downstream_closed'] = not downstream_open
                    response_record.update(response_projection(b''.join(chunks)))
                    response_record['time'] = time.time()
        except Exception as exc:
            response_record['transport_error_type'] = type(exc).__name__
        finally:
            append(OUT / 'provider-responses.jsonl', response_record)

    def log_message(self, *_args):
        pass


class Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


def main():
    # Irreversible one-shot marker prevents accidental second paid sample.
    with (OUT / 'LAUNCH_STARTED').open('x') as f:
        f.write(str(time.time()) + '\n')
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() == '0885c874b949792723c515ff098c24d31cd238c9'
    WORK.mkdir(exist_ok=False)
    ws = WORK / 'workspace'
    shutil.copytree(ROOT / 'docs/evidence/autonomous-comparison-2026-09-15/initial/N', ws)
    initial = hashes(ws)
    assert initial == json.loads((OLD / 'N/nzcoder/initial-hashes.json').read_text())
    save(OUT / 'initial-hashes.json', initial)
    (WORK / 'home').mkdir()
    (WORK / 'bin').mkdir()
    (WORK / 'bin/node').symlink_to(NODE)
    (WORK / 'bin/python').symlink_to(sys.executable)
    values = {}
    for line in (ROOT / '.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip().strip('\"\'')
    credentials = values['API_KEY'], values.get('API_BASE_URL', 'https://api.deepseek.com').rstrip('/')
    task = json.loads((OLD / 'N/nzcoder/command.json').read_text())['env']['TASK_PROMPT']
    save(OUT / 'connection.json', {'base_url': credentials[1], 'credentials': 'loaded privately; omitted'})
    with tempfile.TemporaryDirectory(prefix='nz-real-n-uds-') as td:
        sock = Path(td) / 'provider.sock'
        server = Server(str(sock), Handler)
        server.credentials = credentials
        server.lock = threading.RLock()
        server.main = server.aux = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {'HOME': str(WORK / 'home'), 'KODAX_HOME': str(WORK / 'home/.kodax'),
               'PATH': f'{WORK}/bin:{Path(sys.executable).parent}:/usr/bin:/bin',
               'LANG': 'C.UTF-8', 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'SMOKE_SOCKET': str(sock),
               'SMOKE_OUTPUT': str(OUT), 'TERM': 'dumb', 'NO_COLOR': '1', 'CASE': 'N', 'TASK_PROMPT': task}
        argv = [sys.executable, str(ROOT / 'tests/evaluation/fixtures/offline_exec.py'),
                sys.executable, str(OUT / 'run-nz.py')]
        save(OUT / 'command.json', {'argv': argv, 'cwd': str(ws), 'env': env})
        started = time.monotonic()
        try:
            result = subprocess.run(argv, cwd=ws, env=env, capture_output=True, text=True, timeout=600)
            (OUT / 'stdout.txt').write_text(result.stdout)
            (OUT / 'stderr.txt').write_text(result.stderr)
            save(OUT / 'process.json', {'exit_code': result.returncode, 'elapsed_s': time.monotonic() - started})
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
            save(OUT / 'request-counts.json', {'main': server.main, 'auxiliary': server.aux, 'cost': 'cost unknown'})
            save(OUT / 'final-hashes.json', hashes(ws))
            shutil.copytree(ws, OUT / 'final-files', ignore=shutil.ignore_patterns('.agent', '.nz-coder', '.git'))
    print(json.dumps({'main': server.main, 'auxiliary': server.aux, 'output': str(OUT)}))


if __name__ == '__main__':
    main()
