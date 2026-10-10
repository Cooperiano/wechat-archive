"""Read-only, authenticated loopback pagination. No arbitrary filesystem routes."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit
from .storage import safe_sqlite_files

WEB = Path(__file__).parent / 'web'


def connect(path):
    safe_sqlite_files(path)
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def page_query(conn, table, columns, where, values, order, page):
    total = conn.execute('SELECT count(*) FROM ' + table + ' WHERE ' + where, values).fetchone()[0]
    page = min(page, max(0, (total - 1) // 100))
    rows = conn.execute('SELECT ' + columns + ' FROM ' + table + ' WHERE ' + where +
                        ' ORDER BY ' + order + ' LIMIT 100 OFFSET ?', [*values, page * 100]).fetchall()
    return {'total': total, 'page': page, 'page_size': 100, 'rows': [dict(row) for row in rows]}


def literal_search(value):
    return '%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


class ArchiveServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, archive, port=0):
        self.archive = Path(archive).expanduser().resolve()
        if not self.archive.is_file():
            raise ValueError('archive_missing')
        with closing(connect(self.archive)) as conn:
            conn.execute('SELECT room_id, message_key FROM messages LIMIT 0')
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.host = f'127.0.0.1:{self.server_port}'
        self.origin = 'http://' + self.host
        self.url = self.origin + '/#' + self.token


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, data, content_type='application/json; charset=utf-8'):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get('Host') != self.server.host:
            return self.respond(403, {'error': 'host_rejected'})
        origin = self.headers.get('Origin')
        if origin and origin != self.server.origin:
            return self.respond(403, {'error': 'origin_rejected'})
        url = urlsplit(self.path)
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if url.path in assets:
            name, mime = assets[url.path]
            return self.respond(200, (WEB / name).read_bytes(), mime)
        expected = 'Bearer ' + self.server.token
        if not secrets.compare_digest(self.headers.get('Authorization', ''), expected):
            return self.respond(401, {'error': 'authentication_required'})
        try:
            args = parse_qs(url.query, max_num_fields=20)
            get = lambda key, default='': args.get(key, [default])[0]
            page = int(get('page', '0'))
            if page < 0 or page > 100000:
                raise ValueError()
            search = get('q')
            if len(search) > 500:
                raise ValueError()
            with closing(connect(self.server.archive)) as conn:
                if url.path == '/api/rooms':
                    where, values = '1', []
                    if search:
                        where += " AND name LIKE ? ESCAPE '\\'"
                        values.append(literal_search(search))
                    kind = get('kind')
                    if kind:
                        where += ' AND kind=?'; values.append(kind)
                    result = page_query(conn, 'groups', 'room_id,name,kind,source_rows', where, values,
                                        'source_rows DESC,room_id', page)
                elif url.path == '/api/messages':
                    where, values = 'room_id=?', [get('room')]
                    if search:
                        where += " AND (content LIKE ? ESCAPE '\\' OR sender_name LIKE ? ESCAPE '\\')"
                        values.extend([literal_search(search)] * 2)
                    if get('type'):
                        where += ' AND local_type=?'; values.append(int(get('type')))
                    for name, operator in [('from', '>='), ('before', '<')]:
                        if get(name):
                            where += ' AND create_time' + operator + '?'; values.append(int(get(name)))
                    result = page_query(conn, 'messages', 'message_key,sender_name,create_time,local_type,content,parse_state',
                                        where, values, 'create_time DESC,local_id DESC,message_key', page)
                elif url.path == '/api/status':
                    status = self.server.archive.with_name('status.json')
                    meta = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archive_meta'").fetchone()
                    current = conn.execute("SELECT value FROM archive_meta WHERE key='status'").fetchone() if meta else None
                    result = json.loads(current[0]) if current else json.loads(status.read_text(encoding='utf-8')) if status.is_file() else {
                        'state': 'existing_archive', 'conversations': conn.execute('SELECT count(*) FROM groups').fetchone()[0],
                        'archived_messages': conn.execute('SELECT count(*) FROM messages').fetchone()[0]}
                else:
                    return self.respond(404, {'error': 'not_found'})
            self.respond(200, result)
        except (ValueError, sqlite3.Error, OSError):
            self.respond(400, {'error': 'query_failed'})


def serve(archive, port=0):
    with ArchiveServer(archive, port) as server:
        print('本地页面地址（仅当前启动有效，请勿分享）：' + server.url, flush=True)
        print('在浏览器打开以上地址；按 Ctrl+C 停止。', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
