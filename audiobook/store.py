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
from audiobook.config import BOOKMARKS_FILE, COLLECTIONS_FILE, FINISHED_FILE, LIBRARIES_FILE, METADATA_FILE, PLAYLISTS_FILE, PLAYQUEUE_FILE, PREFS_FILE, PROGRESS_FILE, USERS_FILE, log
from audiobook.passwords import hash_password

def load_users():
    try:
        return json.loads(USERS_FILE.read_text())
    except Exception:
        return {"admin": hash_password("changeme")}

def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2))

def load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {}

def load_metadata():
    if METADATA_FILE.exists():
        try:
            return json.loads(METADATA_FILE.read_text())
        except Exception:
            pass
    return {}

def save_metadata(meta):
    METADATA_FILE.write_text(json.dumps(meta, indent=2))

def load_prefs():
    if PREFS_FILE.exists():
        try:
            return json.loads(PREFS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_prefs(prefs):
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))

def load_bookmarks():
    if BOOKMARKS_FILE.exists():
        try:
            return json.loads(BOOKMARKS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_bookmarks(bm):
    BOOKMARKS_FILE.write_text(json.dumps(bm, indent=2))

def load_collections():
    if COLLECTIONS_FILE.exists():
        try:
            return json.loads(COLLECTIONS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_collections(cols):
    COLLECTIONS_FILE.write_text(json.dumps(cols, indent=2))

def load_finished():
    if FINISHED_FILE.exists():
        try:
            return json.loads(FINISHED_FILE.read_text())
        except Exception:
            pass
    return {}

def save_finished(data):
    FINISHED_FILE.write_text(json.dumps(data, indent=2))

def _is_auto_bookmark(mark):
    if mark.get("manual"):
        return False
    return bool(mark.get("auto")) or mark.get("note") == "auto"

def save_progress():
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))

BACKUP_DIR = ROOT / "backups"

_LAST_BACKUP_DAY = None

def daily_backup():
    """Copy data files into backups/ once per day. Keeps 14 days."""
    global _LAST_BACKUP_DAY
    today = time.strftime("%Y-%m-%d")
    if _LAST_BACKUP_DAY == today:
        return
    marker = BACKUP_DIR / f".done-{today}"
    if marker.exists():
        _LAST_BACKUP_DAY = today
        return
    files = [PROGRESS_FILE, USERS_FILE, METADATA_FILE, PREFS_FILE, BOOKMARKS_FILE, COLLECTIONS_FILE, FINISHED_FILE, LIBRARIES_FILE, PLAYLISTS_FILE]
    for src in files:
        if src.exists():
            dest = BACKUP_DIR / f"{src.stem}-{today}{src.suffix}"
            try:
                dest.write_bytes(src.read_bytes())
            except Exception as e:
                log.warning(f"Backup failed for {src.name}: {e}")
    try:
        marker.write_text(today)
    except Exception:
        pass
    # Prune backups older than 14 days
    cutoff = time.time() - 14 * 86400
    for old in BACKUP_DIR.glob("*-????-??-??.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except Exception:
            pass
    _LAST_BACKUP_DAY = today
    log.info(f"Daily backup written to {BACKUP_DIR}")

def load_extra_libraries():
    if LIBRARIES_FILE.exists():
        try:
            data = json.loads(LIBRARIES_FILE.read_text())
            if isinstance(data, list):
                return [str(p) for p in data if str(p).strip()]
        except Exception:
            pass
    return []

def save_extra_libraries(paths):
    LIBRARIES_FILE.write_text(json.dumps(paths, indent=2))

def load_playqueue():
    if PLAYQUEUE_FILE.exists():
        try:
            data = json.loads(PLAYQUEUE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}

def save_playqueue():
    PLAYQUEUE_FILE.write_text(json.dumps(playqueues))

def load_playlists():
    if PLAYLISTS_FILE.exists():
        try:
            data = json.loads(PLAYLISTS_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}

def save_playlists():
    PLAYLISTS_FILE.write_text(json.dumps(playlists, indent=2))

if not USERS_FILE.exists():
    USERS_FILE.write_text(json.dumps({"admin": hash_password("changeme")}, indent=2))

progress = load_progress()
metadata = load_metadata()
prefs = load_prefs()
bookmarks = load_bookmarks()
collections = load_collections()
finished = load_finished()
BACKUP_DIR.mkdir(exist_ok=True)
daily_backup()
playqueues = load_playqueue()
playlists = load_playlists()
