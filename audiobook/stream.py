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
from audiobook.library import resolve_lib, resolve_music

def send_image_bytes(handler, data, mime):
    handler.send_response(200)
    handler.send_header("Content-Type", mime or "image/jpeg")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)

def cover_mime_for(path):
    ext = path.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    try:
        head = path.read_bytes()[:16]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if head[:2] == b"\xff\xd8":
            return "image/jpeg"
        if head[:4] == b"RIFF":
            return "image/webp"
    except Exception:
        pass
    return "image/jpeg"

def send_zip_paths(handler, title, packed):
    if not packed:
        handler.send_error(404)
        return
    used = set()
    out = []
    for name, path, size in packed:
        arc = name.replace("\\", "/").split("/")[-1] or "audio"
        base = arc
        n = 2
        while arc in used:
            stem = Path(base).stem
            ext = Path(base).suffix
            arc = f"{stem}-{n}{ext}"
            n += 1
        used.add(arc)
        out.append((arc, path, size))
    packed = out
    total = 22
    for arc, _path, size in packed:
        nlen = len(arc.encode())
        total += 30 + nlen + size + 16 + 46 + nlen
    safe_title = (title or "album").replace('"', "")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Length", str(total))
    handler.send_header("Content-Disposition", f'attachment; filename="{safe_title}.zip"')
    handler.end_headers()
    if handler.command == "HEAD":
        return
    stamp = time.localtime()
    dos_time = (stamp.tm_hour << 11) | (stamp.tm_min << 5) | (stamp.tm_sec // 2)
    dos_date = ((stamp.tm_year - 1980) << 9) | (stamp.tm_mon << 5) | stamp.tm_mday
    offset = 0
    central = []
    for arc, path, size in packed:
        name_b = arc.encode()
        handler.wfile.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x0008, 0, dos_time, dos_date, 0, 0, 0, len(name_b), 0))
        handler.wfile.write(name_b)
        hasher = zlib.crc32(b"")
        remaining = size
        with open(path, "rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                hasher = zlib.crc32(chunk, hasher)
                handler.wfile.write(chunk)
                remaining -= len(chunk)
        crc = hasher & 0xFFFFFFFF
        handler.wfile.write(struct.pack("<IIII", 0x08074B50, crc, size, size))
        central.append((name_b, crc, size, offset))
        offset += 30 + len(name_b) + size + 16
    central_start = offset
    for name_b, crc, size, local_off in central:
        handler.wfile.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x0008, 0, dos_time, dos_date, crc, size, size, len(name_b), 0, 0, 0, 0, 0, local_off))
        handler.wfile.write(name_b)
        offset += 46 + len(name_b)
    handler.wfile.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), offset - central_start, central_start, 0))

def send_music_album_zip(handler, album):
    entries = []
    for info in album.get("files") or []:
        path = resolve_music(info.get("rel") or "")
        if path and path.is_file():
            entries.append((info.get("name") or path.name, path, path.stat().st_size))
    send_zip_paths(handler, album.get("name") or "album", entries)

def send_song_file(handler, book, index, download):
    files = book.get("files") or []
    if index is None or index < 0 or index >= len(files):
        handler.send_error(404)
        return
    file_path, _root, _idx = resolve_lib(files[index].get("rel") or "")
    if file_path is None or not file_path.is_file():
        handler.send_error(404)
        return
    handler.stream_file(file_path, as_download=download)

def send_book_zip(handler, book):
    entries = []
    for info in book.get("files") or []:
        path, _root, _idx = resolve_lib(info.get("rel") or "")
        if path and path.is_file():
            entries.append((info.get("name") or path.name, path, path.stat().st_size))
    send_zip_paths(handler, book.get("name") or "book", entries)
