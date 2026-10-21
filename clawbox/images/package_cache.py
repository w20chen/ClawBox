"""Local build cache: official PyPI indexes, mirrored bytes, verified hashes.

Run on the build host and use its loopback URL only in host-network builds.
This does not replace task sources or choose different dependency versions.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import requests


def index_html(data: dict) -> bytes:
    links = []
    for entry in data['files']:
        digest = entry.get('hashes', {}).get('sha256')
        url = urlsplit(entry['url'])
        if not digest or not re.fullmatch('[0-9a-f]{64}', digest):
            continue
        if url.scheme != 'https' or url.hostname != 'files.pythonhosted.org' or not url.path.startswith('/packages/'):
            continue
        path = quote(unquote(url.path), safe='/')
        attributes = ''
        if entry.get('requires-python'):
            attributes += ' data-requires-python="' + html.escape(entry['requires-python'], quote=True) + '"'
        if entry.get('yanked'):
            attributes += ' data-yanked="' + html.escape(str(entry['yanked']), quote=True) + '"'
        links.append(f'<a href="/files/{digest}{path}#sha256={digest}"{attributes}>'
                     + html.escape(entry['filename']) + '</a>')
    return ('<!DOCTYPE html><html><body>' + '\n'.join(links) + '</body></html>').encode()


class Cache:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.locks: dict[str, threading.Lock] = {}
        self.downloads = threading.BoundedSemaphore(12)

    def item_lock(self, key: str):
        with self.lock:
            return self.locks.setdefault(key, threading.Lock())

    def index(self, project: str) -> bytes:
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', project):
            raise ValueError('invalid project name')
        dest = self.root / ('index-' + project + '.json')
        with self.item_lock('index:' + project):
            if not dest.exists() or time.time() - dest.stat().st_mtime > 3600:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.get(f'https://pypi.org/simple/{project}/',
                        headers={'Accept': 'application/vnd.pypi.simple.v1+json'}, timeout=(10, 60))
                    response.raise_for_status()
                    data = response.json()
                temp = dest.with_suffix('.part')
                temp.write_text(json.dumps(data))
                temp.replace(dest)
            return index_html(json.loads(dest.read_text()))

    def artifact(self, digest: str, path: str) -> Path:
        if not re.fullmatch('[0-9a-f]{64}', digest) or not path.startswith('/packages/'):
            raise ValueError('invalid package cache path')
        # Paths are opaque upstream paths; never use them as local filenames.
        if '..' in path.split('/') or '?' in path or '#' in path:
            raise ValueError('invalid package URL')
        dest = self.root / digest
        with self.item_lock(digest):
            if dest.exists():
                return dest  # Only verified, atomically renamed files enter cache.
            if shutil.disk_usage(self.root).free < 80 * 1024**3:
                raise RuntimeError('package cache stopped at disk reserve')
            partial = dest.with_suffix('.part')
            routes = ['https://mirrors.aliyun.com/pypi', 'https://files.pythonhosted.org'] * 2
            with self.downloads, requests.Session() as session:
                session.trust_env = False
                for attempt, origin in enumerate(routes):
                    try:
                        offset = partial.stat().st_size if partial.exists() else 0
                        headers = {'Range': f'bytes={offset}-'} if offset else {}
                        with session.get(origin + path, headers=headers, stream=True, timeout=(10, 60)) as response:
                            response.raise_for_status()
                            if response.status_code != 206:
                                offset = 0
                            elif not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                                raise RuntimeError('inconsistent package range')
                            with partial.open('ab' if offset else 'wb') as stream:
                                for chunk in response.iter_content(1024 * 1024):
                                    stream.write(chunk)
                        with partial.open('rb') as stream:
                            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                        if actual != digest:
                            partial.unlink(missing_ok=True)
                            raise RuntimeError('package bytes differ from official PyPI hash')
                        partial.replace(dest)
                        print(json.dumps({'event': 'verified-package', 'sha256': digest,
                                          'bytes': dest.stat().st_size, 'origin': origin}), flush=True)
                        return dest
                    except Exception:
                        if attempt == len(routes) - 1:
                            raise
                        time.sleep(2)
        raise RuntimeError('unreachable download state')


def serve(root: Path, port: int) -> None:
    cache = Cache(root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                if path == '/health':
                    content = b'{"status":"ok"}'
                elif path.startswith('/simple/'):
                    content = cache.index(path.removeprefix('/simple/').strip('/'))
                elif path.startswith('/files/'):
                    digest, upstream = path.removeprefix('/files/').split('/', 1)
                    artifact = cache.artifact(digest, '/' + upstream)
                    self.send_response(200)
                    self.send_header('Content-Length', str(artifact.stat().st_size))
                    self.end_headers()
                    with artifact.open('rb') as stream:
                        shutil.copyfileobj(stream, self.wfile)
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html' if path.startswith('/simple/') else 'application/json')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                print(json.dumps({'event': 'request-failed', 'path': self.path, 'error': str(exc)}), flush=True)
                self.send_error(502, 'upstream download or verification failed')

        def log_message(self, format, *args):
            pass

    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=17900)
    args = parser.parse_args()
    serve(args.root, args.port)
