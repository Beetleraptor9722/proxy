#!/usr/bin/env python3
"""
secure_proxy_no_tls.py
HTTPS-capable proxy (supports CONNECT) with Basic auth + PBKDF2 password storage.
Can run in two modes:
 - TLS mode (wrap server socket with provided cert/key)
 - TLS-offload mode (no TLS on server socket) -- for use behind hosting TLS termination
"""

import argparse
import base64
import hashlib
import os
import secrets
import sqlite3
import socket
import ssl
import select
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
PBKDF2_ITER = 200_000

# -----------------------
# User store (sqlite)
# -----------------------
def init_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY,
        salt TEXT NOT NULL,
        hash TEXT NOT NULL,
        iterations INTEGER NOT NULL,
        algo TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def create_user(username: str, password: str, path=DB_PATH):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITER)
    h = dk.hex()
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users(username, salt, hash, iterations, algo) VALUES (?,?,?,?,?)",
                (username, salt, h, PBKDF2_ITER, "pbkdf2_sha256"))
    conn.commit()
    conn.close()

def verify_user(username: str, password: str, path=DB_PATH) -> bool:
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("SELECT salt, hash, iterations, algo FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False
    salt, stored_hash, iterations, algo = row
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return dk.hex() == stored_hash

# -----------------------
# Auth helpers
# -----------------------
def parse_basic_auth(header_value: str):
    if not header_value:
        return None, None
    parts = header_value.split()
    if len(parts) != 2:
        return None, None
    scheme, token = parts
    if scheme.lower() != "basic":
        return None, None
    try:
        decoded = base64.b64decode(token).decode(errors="ignore")
        if ":" in decoded:
            user, pw = decoded.split(":", 1)
            return user, pw
    except Exception:
        return None, None
    return None, None

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade"
}

# -----------------------
# Proxy handler
# -----------------------
class ProxyHandler(BaseHTTPRequestHandler):
    timeout = 15
    server_version = "SimpleSecureProxy/0.2"

    def log_message(self, format, *args):
        print("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format%args))

    def do_AUTH_REQUIRED(self):
        self.send_response(407, "Proxy Authentication Required")
        self.send_header("Proxy-Authenticate", 'Basic realm="SecureProxy"')
        self.end_headers()

    def check_auth(self):
        auth = self.headers.get("Proxy-Authorization")
        if not auth:
            return False
        user, pw = parse_basic_auth(auth)
        if not user:
            return False
        return verify_user(user, pw)

    def do_CONNECT(self):
        # CONNECT host:port -> create raw TCP tunnel
        if not self.check_auth():
            self.do_AUTH_REQUIRED()
            return
        host, _, port = self.path.rpartition(":")
        port = int(port) if port else 443
        try:
            remote = socket.create_connection((host, port), timeout=self.timeout)
        except Exception as e:
            self.send_error(502, "Bad gateway: %s" % e)
            return

        # reply OK
        self.send_response(200, "Connection Established")
        self.end_headers()

        client_sock = self.connection
        remote.setblocking(False)
        client_sock.setblocking(False)

        try:
            while True:
                rlist, _, _ = select.select([client_sock, remote], [], [], self.timeout)
                if not rlist:
                    break
                if client_sock in rlist:
                    data = client_sock.recv(8192)
                    if not data:
                        break
                    remote.sendall(data)
                if remote in rlist:
                    data = remote.recv(8192)
                    if not data:
                        break
                    client_sock.sendall(data)
        finally:
            try: remote.close()
            except: pass
            try: client_sock.close()
            except: pass

    def _filter_headers(self, headers):
        return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}

    def _do_forward(self):
        if not self.check_auth():
            self.do_AUTH_REQUIRED()
            return

        parsed = urlsplit(self.path)
        if parsed.scheme and parsed.netloc:
            target_host = parsed.hostname
            target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
        else:
            host_hdr = self.headers.get("Host")
            if not host_hdr:
                self.send_error(400, "No Host header")
                return
            host_only, _, port_part = host_hdr.partition(":")
            target_host = host_only
            target_port = int(port_part) if port_part else 80
            path = self.path

        try:
            conn = socket.create_connection((target_host, target_port), timeout=self.timeout)
        except Exception as e:
            self.send_error(502, "Bad gateway: %s" % e)
            return

        try:
            req_line = f"{self.command} {path} {self.request_version}\r\n"
            conn.sendall(req_line.encode())

            headers = self._filter_headers(self.headers)
            headers.pop("Proxy-Authorization", None)
            if "Host" not in headers:
                headers["Host"] = target_host

            for k, v in headers.items():
                conn.sendall(f"{k}: {v}\r\n".encode())
            conn.sendall(b"\r\n")

            if 'Content-Length' in self.headers:
                length = int(self.headers['Content-Length'])
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(8192, remaining))
                    if not chunk:
                        break
                    conn.sendall(chunk)
                    remaining -= len(chunk)

            self.connection.settimeout(self.timeout)
            while True:
                data = conn.recv(8192)
                if not data:
                    break
                self.connection.sendall(data)
        finally:
            try: conn.close()
            except: pass

    def do_GET(self):   self._do_forward()
    def do_POST(self):  self._do_forward()
    def do_PUT(self):   self._do_forward()
    def do_DELETE(self):self._do_forward()
    def do_OPTIONS(self):self._do_forward()
    def do_HEAD(self):  self._do_forward()
    def do_PATCH(self): self._do_forward()

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def run_server(host, port, certfile, keyfile, tls_offload):
    init_db()
    server = ThreadedHTTPServer((host, port), ProxyHandler)

    if tls_offload:
        print("Running in TLS-offload mode (no TLS on this process). Make sure hosting handles TLS or you're behind a trusted LB.")
    else:
        if not certfile or not keyfile:
            raise ValueError("cert and key must be provided unless --tls-offload is set")
        if not os.path.exists(certfile) or not os.path.exists(keyfile):
            raise FileNotFoundError("Cert or key file not found")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        print(f"Wrapped socket with TLS cert {certfile}")

    print(f"Starting proxy on {host}:{port} (tls_offload={tls_offload})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down")
        server.server_close()

# -----------------------
# CLI
# -----------------------
def main():
    global DB_PATH
    parser = argparse.ArgumentParser(description="Secure proxy (CONNECT) with optional TLS offload")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1 for safety)")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--cert", help="TLS cert (PEM). Required if not using --tls-offload")
    parser.add_argument("--key", help="TLS key (PEM). Required if not using --tls-offload")
    parser.add_argument("--tls-offload", action="store_true", help="Run without TLS (for hosting TLS-termination)")
    parser.add_argument("--add-user", dest="add_user", help="Add user (will prompt for password)")
    parser.add_argument("--db", default=DB_PATH, help="Path to users DB")
    args = parser.parse_args()

    DB_PATH = args.db
    init_db(DB_PATH)

    if args.add_user:
        import getpass
        pw = getpass.getpass("Password for %s: " % args.add_user)
        create_user(args.add_user, pw, DB_PATH)
        print("User created.")
        return

    # If user requests tls-offload, ensure host default is localhost for safety
    if args.tls_offload and args.host == "0.0.0.0":
        print("Warning: running tls-offload on 0.0.0.0 is insecure; recommended bind to 127.0.0.1")

    run_server(args.host, args.port, args.cert, args.key, args.tls_offload)

if __name__ == "__main__":
    main()
