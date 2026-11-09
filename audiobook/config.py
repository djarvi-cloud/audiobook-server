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


ROOT = Path(__file__).resolve().parent.parent

PORT = int(os.environ.get("PORT", 17930))

AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", Path.home() / "files" / "audiobooks"))

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "files" / "music")).expanduser()

USERS_FILE = ROOT / "users.json"

PROGRESS_FILE = ROOT / "progress.json"

METADATA_FILE = ROOT / "metadata.json"

PREFS_FILE = ROOT / "prefs.json"

BOOKMARKS_FILE = ROOT / "bookmarks.json"

COLLECTIONS_FILE = ROOT / "collections.json"

FINISHED_FILE = ROOT / "finished.json"

LIBRARIES_FILE = ROOT / "libraries.json"

SEAL_FILE = ROOT / "subsonic-auth.json"

SEAL_KEY_FILE = ROOT / "subsonic.key"

PLAYQUEUE_FILE = ROOT / "playqueue.json"

PLAYLISTS_FILE = ROOT / "playlists.json"

COVER_CACHE = ROOT / "covers"

MUSIC_FOLDER_ID = "mf:music"

AUDIO_EXTS = {".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".webm"}

COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg",
               "folder.png", "album.jpg", "front.jpg"}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default

LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()

LOG_FILE = Path(os.environ.get("LOG_FILE", str(ROOT / "audiobook.log")))

LOG_MAX_BYTES = _env_int("LOG_MAX_BYTES", 5 * 1024 * 1024)

LOG_BACKUPS = _env_int("LOG_BACKUPS", 3)

log = logging.getLogger("audiobook")

def setup_logging():
    level = getattr(logging, LOG_LEVEL_NAME, None)
    if not isinstance(level, int):
        level = logging.INFO
    log.handlers.clear()
    log.setLevel(level)
    log.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        rotating = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES if LOG_MAX_BYTES > 0 else 5 * 1024 * 1024,
            backupCount=max(LOG_BACKUPS, 0),
            encoding="utf-8",
        )
        rotating.setFormatter(fmt)
        log.addHandler(rotating)
    except Exception as exc:
        log.warning(f"Could not open log file {LOG_FILE}: {exc}")

_SECRET_QUERY = re.compile(
    r"([?&](?:p|password|pass|t|token|s|salt)=)[^&\s]*",
    re.IGNORECASE,
)

def redact(text):
    if not text:
        return text
    return _SECRET_QUERY.sub(r"\1***", str(text))

def iso_now(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))

setup_logging()
COVER_CACHE.mkdir(exist_ok=True)
mimetypes.add_type("audio/mp4", ".m4b")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("audio/aac", ".aac")
