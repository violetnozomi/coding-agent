"""复用上批回环官方层缓存方式；只提供第9题已验证对象，无网络转发。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import json

OUT = Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch4-2026-10-12')
receipt = json.loads((OUT / 'measured-image-9.json').read_text())
assert receipt['all_compressed_and_uncompressed_hashes_verified']
folder = Path(receipt['temporary_cache'])
raw = (folder / 'manifest.json').read_bytes()
digest = receipt['official_manifest_digest']
assert 'sha256:' + hashlib.sha256(raw).hexdigest() == digest
manifest = json.loads(raw)
objects = {item['digest']: item for item in manifest['layers'] + [manifest['config']]}
for name, item in objects.items():
    path = folder / name.split(':')[1]
    assert path.stat().st_size == item['size']
tag = json.loads((OUT / 'task-manifest.json').read_text())['tasks'][8]['configuration']['environment']['docker_image'].split(':', 1)[1]
prefix = '/v2/scaleapi/swe-bench_pro-v2/'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.serve(False)

    def do_GET(self):
        self.serve(True)

    def serve(self, body):
        if self.path == '/v2/':
            data, mime, blob = b'{}', 'application/json', None
        elif self.path in [prefix + 'manifests/' + tag, prefix + 'manifests/' + digest]:
            data, mime, blob = raw, manifest['mediaType'], None
        elif self.path.startswith(prefix + 'blobs/') and self.path[len(prefix + 'blobs/'):] in objects:
            name = self.path[len(prefix + 'blobs/'):]
            data, mime, blob = None, 'application/octet-stream', folder / name.split(':')[1]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(blob.stat().st_size if blob else len(data)))
        if self.path.startswith(prefix + 'manifests/'):
            self.send_header('Docker-Content-Digest', digest)
        self.end_headers()
        if body:
            if blob:
                with blob.open('rb') as source:
                    while block := source.read(1024 ** 2):
                        self.wfile.write(block)
            else:
                self.wfile.write(data)


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
(OUT / 'image-9-local-endpoint.json').write_text(json.dumps({
    'host': '127.0.0.1', 'port': server.server_port, 'fixed_manifest_digest': digest,
    'tag': tag, 'only_verified_official_objects': True, 'outbound_requests': 0,
    'model_calls': 0,
}, indent=2) + '\n')
print('cache_ready', server.server_port, flush=True)
server.serve_forever()
