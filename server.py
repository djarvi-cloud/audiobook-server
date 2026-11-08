#!/usr/bin/env python3
import os
import json
import base64
import mimetypes
import secrets
import time
import traceback
import hashlib
import struct
import zlib
import random
import urllib.request
import urllib.parse
import ssl
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

PORT = int(os.environ.get("PORT", 17930))
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", Path.home() / "files" / "audiobooks"))
USERS_FILE = Path(__file__).parent / "users.json"
PROGRESS_FILE = Path(__file__).parent / "progress.json"
METADATA_FILE = Path(__file__).parent / "metadata.json"
PREFS_FILE = Path(__file__).parent / "prefs.json"
BOOKMARKS_FILE = Path(__file__).parent / "bookmarks.json"
COLLECTIONS_FILE = Path(__file__).parent / "collections.json"
FINISHED_FILE = Path(__file__).parent / "finished.json"
LIBRARIES_FILE = Path(__file__).parent / "libraries.json"
SEAL_FILE = Path(__file__).parent / "subsonic-auth.json"
SEAL_KEY_FILE = Path(__file__).parent / "subsonic.key"
PLAYQUEUE_FILE = Path(__file__).parent / "playqueue.json"
COVER_CACHE = Path(__file__).parent / "covers"
COVER_CACHE.mkdir(exist_ok=True)
SESSIONS = {}
BOOKS_CACHE = {"data": None, "mtime": 0}
COVER_FETCH_LOCK = threading.Lock()
COVER_FETCHING = set()

# ... (full 3640-line content of server.py continues here in the actual call)