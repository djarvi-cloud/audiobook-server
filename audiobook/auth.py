import os
import json
import base64
import mimetypes
import secrets
import time
import hashlib
import struct
import zlib
import random
import re
import urllib.request
import urllib.parse
import ssl
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from audiobook.config import ROOT
from audiobook.config import SEAL_FILE, SEAL_KEY_FILE
from audiobook.passwords import verify_password
from audiobook.store import load_users

def q1(qs, key, default=""):
    vals = qs.get(key) or []
    return vals[0] if vals else default

def q_bool(qs, key, default=None):
    raw = q1(qs, key, "")
    if raw == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")

SESSIONS = {}

def seal_key():
    if not SEAL_KEY_FILE.exists():
        SEAL_KEY_FILE.write_bytes(secrets.token_bytes(32))
    return SEAL_KEY_FILE.read_bytes()

def seal_password(password: str) -> str:
    raw = password.encode()
    stream = b""
    n = 0
    while len(stream) < len(raw):
        stream += hashlib.sha256(seal_key() + n.to_bytes(4, "big")).digest()
        n += 1
    return base64.b64encode(bytes(a ^ b for a, b in zip(raw, stream))).decode()

def unseal_password(token: str) -> str:
    raw = base64.b64decode(token)
    stream = b""
    n = 0
    while len(stream) < len(raw):
        stream += hashlib.sha256(seal_key() + n.to_bytes(4, "big")).digest()
        n += 1
    return bytes(a ^ b for a, b in zip(raw, stream)).decode()

def load_seals():
    if SEAL_FILE.exists():
        try:
            data = json.loads(SEAL_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}

def remember_password(username, password):
    if not username or password is None:
        return
    seals = load_seals()
    seals[username] = seal_password(password)
    SEAL_FILE.write_text(json.dumps(seals))

def recall_password(username):
    token = load_seals().get(username)
    if not token:
        return None
    try:
        return unseal_password(token)
    except Exception:
        return None

def forget_password(username):
    seals = load_seals()
    if username in seals:
        seals.pop(username, None)
        SEAL_FILE.write_text(json.dumps(seals))

def get_session(handler):
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session="):
            token = part.split("=", 1)[1].strip()
            sess = SESSIONS.get(token)
            if sess and time.time() - sess["created"] < 60 * 60 * 24 * 7:
                return token, sess
    return None, None

def load_https_redirect():
    url = os.environ.get("HTTPS_REDIRECT", "").strip()
    if not url:
        path = ROOT / "https-url.txt"
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    url = line
                    break
    if not url:
        url = "https://audiobooks.covertsa.com"
    if url and not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url.rstrip("/")

def public_https_host():
    if not HTTPS_REDIRECT:
        return ""
    return (urlparse(HTTPS_REDIRECT).hostname or "").lower()

def is_https(handler):
    proto = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    if handler.headers.get("X-Forwarded-Ssl", "").lower() == "on":
        return True
    host = (handler.headers.get("Host") or "").split(":")[0].lower()
    pub = public_https_host()
    if pub and host == pub:
        return True
    try:
        return isinstance(handler.connection, ssl.SSLSocket)
    except Exception:
        return False

def maybe_redirect_https(handler):
    """Send browsers on plain HTTP to the public HTTPS URL. Already-secure requests stay."""
    if not HTTPS_REDIRECT or is_https(handler):
        return False
    target = handler.path or "/"
    if "\r" in target or "\n" in target or not target.startswith("/"):
        target = "/"
    handler.send_response(302)
    handler.send_header("Location", HTTPS_REDIRECT + target)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True

def session_cookie(handler, token="", max_age=0):
    flags = "Path=/; HttpOnly; SameSite=Lax"
    if is_https(handler):
        flags += "; Secure"
    return f"session={token}; {flags}; Max-Age={max_age}"

def decode_subsonic_password(raw):
    if raw.startswith("enc:"):
        try:
            return bytes.fromhex(raw[4:]).decode()
        except Exception:
            return ""
    return raw

def check_user_password(username, password):
    users = load_users()
    stored = users.get(username)
    if stored is None or password is None:
        return False
    if len(stored) > 40:
        return verify_password(password, stored)
    return stored == password

def subsonic_user(qs):
    username = q1(qs, "u").strip()
    if not username or username not in load_users():
        return None, "Wrong username or password"
    token = q1(qs, "t")
    salt = q1(qs, "s")
    if token and salt:
        plain = recall_password(username)
        if not plain:
            return None, "Log in once on the website, then connect the app again"
        digest = hashlib.md5((plain + salt).encode()).hexdigest()
        if digest.lower() != token.lower():
            return None, "Wrong username or password"
        return username, None
    password = decode_subsonic_password(q1(qs, "p"))
    if password and check_user_password(username, password):
        remember_password(username, password)
        return username, None
    return None, "Wrong username or password"

HTTPS_REDIRECT = load_https_redirect()
