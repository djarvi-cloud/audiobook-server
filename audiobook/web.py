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
from audiobook.config import AUDIO_DIR, COVER_CACHE, LOG_FILE, MUSIC_DIR, PORT, USERS_FILE, log, redact
from audiobook.auth import HTTPS_REDIRECT, SESSIONS, forget_password, get_session, maybe_redirect_https, remember_password, session_cookie
from audiobook.passwords import hash_password, verify_password
from audiobook.store import _is_auto_bookmark, bookmarks, collections, daily_backup, finished, load_extra_libraries, load_users, metadata, prefs, progress, save_bookmarks, save_collections, save_extra_libraries, save_finished, save_metadata, save_prefs, save_progress, save_users
from audiobook.library import BOOKS_CACHE, COVER_FETCHING, COVER_FETCH_LOCK, attach_file_duration, fetch_cover, fetch_from_isbn, get_book_by_id, get_books_cached, get_music_cached, library_list, library_roots, music_album_by_id, music_as_book, music_cover_file, next_in_series, resolve_item_file, save_remote_cover, scan_folder
from audiobook.stream import cover_mime_for, send_image_bytes
from audiobook.subsonic import handle_subsonic, mutate_playlist, playlist_get, playlist_page_id, playlist_visible, visible_playlists, web_playlist

class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        # Some players send HEAD first
        self.do_GET()

    def do_GET(self):
        if maybe_redirect_https(self):
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/rest/"):
                handle_subsonic(self, parsed)
                return
            token, sess = get_session(self)

            if path == "/login":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(LOGIN_HTML.encode())
                return

            if path == "/logout":
                if token:
                    SESSIONS.pop(token, None)
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Set-Cookie", session_cookie(self, max_age=0))
                self.end_headers()
                return

            if not sess:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return

            username = sess["user"]
            daily_backup()

            if path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                html = HTML.replace("{{IS_ADMIN}}", "true" if username == "admin" else "false")
                self.wfile.write(html.encode())
                return

            if path.startswith("/book/"):
                book_id = urllib.parse.unquote(path[6:])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                html = BOOK_HTML.replace("{{BOOK_ID}}", book_id)
                self.wfile.write(html.encode())
                return

            if path.startswith("/playlist/"):
                pid = path[len("/playlist/"):].split("/")[0]
                if not playlist_page_id(pid):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(PLAYLIST_HTML.replace("{{PLAYLIST_ID}}", pid).encode())
                return

            if path == "/users" and username == "admin":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(USERS_HTML.encode())
                return

            if path == "/password":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(PASSWORD_HTML.encode())
                return

            if path == "/api/collections":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(collections).encode())
                return

            if path == "/api/playlists":
                qs = parse_qs(parsed.query)
                pid = qs.get("id", [""])[0]
                if pid:
                    pl = playlist_get(pid) if playlist_page_id(pid) else None
                    if not pl or not playlist_visible(pl, username):
                        self.send_error(404)
                        return
                    payload = web_playlist(pl, username, with_songs=True)
                else:
                    payload = visible_playlists(username)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
                return

            if path == "/api/libraries":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(library_list()).encode())
                return

            if path == "/api/browse":
                qs = parse_qs(parsed.query)
                rel = qs.get("path", [""])[0]
                scope = qs.get("library", ["books"])[0]
                if scope not in ("books", "music"):
                    scope = "books"
                # Basic path safety
                if ".." in rel or rel.startswith("/"):
                    rel = ""
                data = scan_folder(rel, scope)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
                return

            if path == "/api/rescan":
                get_books_cached(force=True)
                get_music_cached(force=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            if path == "/api/clear-covers":
                qs = parse_qs(parsed.query)
                rel = qs.get("path", [""])[0]
                cleared = 0
                try:
                    books = get_books_cached()
                    for b in books:
                        book_path = b.get("path") or ""
                        if not rel or book_path == rel or book_path.startswith(rel + "/"):
                            cache_file = COVER_CACHE / f"{b['id']}.jpg"
                            if cache_file.exists():
                                cache_file.unlink()
                                cleared += 1
                except Exception as e:
                    log.warning(f"Clear covers error: {e}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"cleared": cleared}).encode())
                return

            if path == "/api/set-meta":
                # handled in POST
                self.send_error(405)
                return

            if path == "/api/books":
                qs = parse_qs(parsed.query)
                scope = qs.get("library", ["books"])[0]
                if scope == "music":
                    books = [music_as_book(a) for a in get_music_cached()]
                else:
                    books = list(get_books_cached() or [])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(books).encode())
                return

            if path.startswith("/api/book/"):
                book_id = urllib.parse.unquote(path[10:])
                log.debug(f"Book lookup: {book_id}")
                book = attach_file_duration(get_book_by_id(book_id))
                if book and book.get("series"):
                    nxt = next_in_series(book)
                    if nxt:
                        book = dict(book)
                        book["nextInSeries"] = nxt
                if not book:
                    log.info(f"Book not found: {book_id}")
                    self.send_error(404)
                    return
                log.debug(f"Book found: {book.get('name')} ({len(book.get('files', []))} files)")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(book).encode())
                return

            if path == "/api/progress":
                user_progress = progress.get(username, {})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(user_progress).encode())
                return

            if path == "/api/clear-progress":
                qs = parse_qs(parsed.query)
                book_id = qs.get("id", [""])[0]
                if book_id and username in progress and book_id in progress[username]:
                    del progress[username][book_id]
                    save_progress()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return

            if path == "/api/prefs":
                user_prefs = prefs.get(username, {})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(user_prefs).encode())
                return

            if path == "/api/bookmarks":
                qs = parse_qs(parsed.query)
                book_id = qs.get("id", [""])[0]
                user_bm = bookmarks.get(username, {}).get(book_id, [])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(user_bm).encode())
                return

            if path == "/api/finished":
                ids = finished.get(username, [])
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(ids).encode())
                return

            if path == "/api/users" and username == "admin":
                users = load_users()
                safe = [{"username": u} for u in users.keys()]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(safe).encode())
                return

            if path.startswith("/stream/"):
                parts = [urllib.parse.unquote(part) for part in path[8:].split("/")]
                book_id = parts[0]
                try:
                    file_index = int(parts[1]) if len(parts) > 1 else 0
                except ValueError:
                    file_index = 0
                qs = parse_qs(parsed.query)
                as_download = qs.get("download", ["0"])[0] == "1"
                book = get_book_by_id(book_id)
                if not book or file_index >= len(book["files"]):
                    log.warning(f"Stream failed: book not found or bad index ({book_id} file {file_index})")
                    self.send_error(404)
                    return
                file_path = resolve_item_file(book, book["files"][file_index]["rel"])
                if file_path is None:
                    log.warning(f"Stream failed: path refused ({book_id} file {file_index})")
                    self.send_error(403)
                    return
                if not file_path.is_file():
                    log.warning(f"Stream failed: file missing {file_path}")
                    self.send_error(404)
                    return
                if self.headers.get("Range"):
                    log.debug(f"Streaming range: {file_path}")
                else:
                    log.info(f"Streaming: {file_path}")
                self.stream_file(file_path, as_download=as_download)
                return

            if path.startswith("/cover/"):
                book_id = urllib.parse.unquote(path[7:])
                book = get_book_by_id(book_id)
                if not book:
                    self.send_error(404)
                    return

                # Prefer custom cached cover if user has set one
                custom_meta = metadata.get(book_id, {})
                custom_cache = COVER_CACHE / f"{book_id}.jpg"
                if custom_meta.get("cover_url") and custom_cache.exists():
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Cache-Control", "public, max-age=300")
                    self.end_headers()
                    with open(custom_cache, "rb") as f:
                        self.wfile.write(f.read())
                    return

                # Then local folder cover
                if book.get("cover"):
                    cover_path = resolve_item_file(book, book["cover"])
                    if cover_path and cover_path.is_file():
                        mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.end_headers()
                        with open(cover_path, "rb") as f:
                            self.wfile.write(f.read())
                        return

                if book.get("kind") == "music":
                    album = music_album_by_id(book_id)
                    path = music_cover_file(album) if album else None
                    if path and path.is_file():
                        send_image_bytes(self, path.read_bytes(), cover_mime_for(path))
                        return
                    self.send_error(404)
                    return

                # Serve already-cached auto cover if present
                cached = COVER_CACHE / f"{book_id}.jpg"
                if not cached.exists() and book.get("coverUrl"):
                    save_remote_cover(book["coverUrl"], book_id)
                if cached.exists():
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    with open(cached, "rb") as f:
                        self.wfile.write(f.read())
                    return

                # No cover yet — kick off a single background fetch (don't spam)
                with COVER_FETCH_LOCK:
                    if book_id not in COVER_FETCHING:
                        COVER_FETCHING.add(book_id)
                        def bg_fetch(bid=book_id, author=book.get("author"), title=book.get("name")):
                            try:
                                fetch_cover(author, title, bid)
                            except Exception:
                                pass
                            finally:
                                with COVER_FETCH_LOCK:
                                    COVER_FETCHING.discard(bid)
                        threading.Thread(target=bg_fetch, daemon=True).start()

                # Return 404 immediately so the page doesn't wait
                self.send_error(404)
                return

            self.send_error(404)
        except Exception as e:
            log.exception("GET error")
            self.send_error(500, str(e))

    def do_POST(self):
        if maybe_redirect_https(self):
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/rest/"):
                handle_subsonic(self, parsed)
                return

            if path == "/login":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                data = parse_qs(body)
                user = data.get("username", [""])[0]
                passwd = data.get("password", [""])[0]
                users = load_users()
                stored = users.get(user)
                ok = False
                if stored:
                    if len(stored) > 40:
                        ok = verify_password(passwd, stored)
                    else:
                        ok = (stored == passwd)
                        if ok:
                            users[user] = hash_password(passwd)
                            save_users(users)
                if ok:
                    log.info(f"Login ok user={user} from {self.address_string()}")
                    remember_password(user, passwd)
                    token = secrets.token_urlsafe(32)
                    SESSIONS[token] = {"created": time.time(), "user": user}
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", session_cookie(self, token, 604800))
                    self.end_headers()
                else:
                    log.warning(f"Login failed user={user or '-'} from {self.address_string()}")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    html = LOGIN_HTML.replace("<!--ERROR-->", "<p style='color:#f66'>Invalid username or password</p>")
                    self.wfile.write(html.encode())
                return

            token, sess = get_session(self)
            if not sess:
                self.send_response(401)
                self.end_headers()
                return

            username = sess["user"]

            if path == "/api/progress":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    if username not in progress:
                        progress[username] = {}
                    progress[username][data["id"]] = {
                        "fileIndex": data.get("fileIndex", 0),
                        "time": data.get("time", 0),
                        "updated": int(time.time() * 1000)
                    }
                    save_progress()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/prefs":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    if username not in prefs:
                        prefs[username] = {}
                    prefs[username].update(data)
                    save_prefs(prefs)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/bookmarks":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    book_id = data.get("id")
                    action = data.get("action", "add")
                    if username not in bookmarks:
                        bookmarks[username] = {}
                    if book_id not in bookmarks[username]:
                        bookmarks[username][book_id] = []
                    if action == "add":
                        note = (data.get("note") or "").strip()
                        fi = int(data.get("fileIndex", 0) or 0)
                        tm = float(data.get("time", 0) or 0)
                        is_auto = bool(data.get("auto"))
                        marks = bookmarks[username][book_id]
                        if is_auto:
                            for existing in reversed(marks):
                                if _is_auto_bookmark(existing) and existing.get("fileIndex") == fi and abs(float(existing.get("time", 0)) - tm) < 90:
                                    self.send_response(200)
                                    self.end_headers()
                                    self.wfile.write(b"ok")
                                    return
                        marks.append({
                            "fileIndex": fi,
                            "time": tm,
                            "note": "auto" if is_auto else note,
                            "auto": is_auto,
                            "manual": not is_auto,
                            "created": int(time.time() * 1000)
                        })
                        if is_auto:
                            autos = [m for m in marks if _is_auto_bookmark(m)]
                            if len(autos) > 10:
                                drop = set(id(m) for m in autos[:-10])
                                bookmarks[username][book_id] = [m for m in marks if id(m) not in drop]
                    elif action == "delete":
                        idx = data.get("index", -1)
                        if 0 <= idx < len(bookmarks[username][book_id]):
                            bookmarks[username][book_id].pop(idx)
                    save_bookmarks(bookmarks)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/finished":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    book_id = (data.get("id") or "").strip()
                    action = data.get("action", "add")
                    if not book_id:
                        self.send_error(400)
                        return
                    ids = finished.get(username, [])
                    if action == "remove":
                        ids = [i for i in ids if i != book_id]
                    elif book_id not in ids:
                        ids.append(book_id)
                    finished[username] = ids
                    save_finished(finished)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(ids).encode())
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/libraries" and username == "admin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    action = data.get("action")
                    raw = (data.get("path") or "").strip()
                    extras = load_extra_libraries()
                    err = None
                    if action == "add":
                        if not raw:
                            err = "Enter a folder path"
                        else:
                            p = Path(raw).expanduser()
                            if not p.is_absolute():
                                err = "Use a full path, like /home/you/files/more-books"
                            else:
                                try:
                                    p = p.resolve()
                                except Exception:
                                    p = None
                                    err = "Cannot read that folder"
                                if p is not None and not p.is_dir():
                                    err = "That folder does not exist"
                                elif p is not None:
                                    known = {str(r) for r in library_roots()}
                                    if str(p) in known:
                                        err = "Already added"
                                    else:
                                        extras.append(str(p))
                                        save_extra_libraries(extras)
                                        BOOKS_CACHE["data"] = None
                    elif action == "remove":
                        try:
                            target = str(Path(raw).expanduser().resolve())
                        except Exception:
                            target = raw
                        if target == str(library_roots()[0]):
                            err = "The original library stays"
                        else:
                            kept = []
                            for item in extras:
                                try:
                                    item_res = str(Path(item).expanduser().resolve())
                                except Exception:
                                    item_res = item
                                if item_res != target and item != raw:
                                    kept.append(item)
                            save_extra_libraries(kept)
                            BOOKS_CACHE["data"] = None
                    else:
                        err = "Unknown action"
                    if err:
                        self.send_response(400)
                        self.send_header("Content-Type", "text/plain")
                        self.end_headers()
                        self.wfile.write(err.encode())
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(library_list()).encode())
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/collections":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    action = data.get("action")
                    name = data.get("name", "").strip()
                    book_id = data.get("book_id", "")
                    if action == "create" and name and name not in collections:
                        collections[name] = []
                    elif action == "delete" and name in collections:
                        del collections[name]
                    elif action == "add" and name in collections and book_id:
                        if book_id not in collections[name]:
                            collections[name].append(book_id)
                    elif action == "remove" and name in collections and book_id:
                        if book_id in collections[name]:
                            collections[name].remove(book_id)
                    save_collections(collections)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/playlists":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except Exception:
                    self.send_error(400)
                    return
                status, payload = mutate_playlist(username, data)
                raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain" if isinstance(payload, str) else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            if path == "/api/users" and username == "admin":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    action = data.get("action")
                    users = load_users()
                    if action == "add":
                        u = data.get("username", "").strip()
                        p = data.get("password", "")
                        if u and p and u not in users:
                            users[u] = hash_password(p)
                            remember_password(u, p)
                            save_users(users)
                    elif action == "change":
                        u = data.get("username", "").strip()
                        p = data.get("password", "")
                        if u in users and p:
                            users[u] = hash_password(p)
                            remember_password(u, p)
                            save_users(users)
                    elif action == "delete":
                        u = data.get("username", "").strip()
                        if u in users and u != "admin":
                            del users[u]
                            forget_password(u)
                            save_users(users)
                            progress.pop(u, None)
                            save_progress()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/password":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    current = data.get("current", "")
                    new = data.get("new", "")
                    users = load_users()
                    stored = users.get(username)
                    if not stored or not verify_password(current, stored):
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b"Current password incorrect")
                        return
                    if len(new) < 4:
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b"New password too short")
                        return
                    users[username] = hash_password(new)
                    remember_password(username, new)
                    save_users(users)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception:
                    self.send_error(400)
                return

            if path == "/api/set-meta":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                    book_id = data.get("id", "").strip()
                    if not book_id:
                        self.send_error(400)
                        return
                    meta = metadata.get(book_id, {})
                    if "title" in data:
                        meta["title"] = data["title"].strip()
                    if "author" in data:
                        meta["author"] = data["author"].strip()
                    if "cover_url" in data and data["cover_url"].strip():
                        cover_url = data["cover_url"].strip()
                        meta["cover_url"] = cover_url

                        # Direct image URL (including Open Library covers) — just download
                        is_direct_image = (
                            cover_url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')) or
                            'covers.openlibrary.org' in cover_url
                        )

                        if not is_direct_image:
                            # Check if it's an ISBN
                            clean = cover_url.replace("-", "").replace(" ", "")
                            if clean.isdigit() and len(clean) in (10, 13):
                                isbn_data = fetch_from_isbn(clean)
                                if isbn_data:
                                    if isbn_data.get("title") and not meta.get("title"):
                                        meta["title"] = isbn_data["title"]
                                    if isbn_data.get("author") and not meta.get("author"):
                                        meta["author"] = isbn_data["author"]
                                    if isbn_data.get("cover_url"):
                                        cover_url = isbn_data["cover_url"]
                            else:
                                # Try Open Library / Google Books page links
                                ol_data = fetch_from_openlibrary_url(cover_url)
                                if not ol_data:
                                    ol_data = fetch_from_googlebooks_url(cover_url)
                                if ol_data:
                                    if ol_data.get("title") and not meta.get("title"):
                                        meta["title"] = ol_data["title"]
                                    if ol_data.get("author") and not meta.get("author"):
                                        meta["author"] = ol_data["author"]
                                    if ol_data.get("cover_url"):
                                        cover_url = ol_data["cover_url"]

                        # Download the cover (whatever URL we ended up with)
                        try:
                            cache_path = COVER_CACHE / f"{book_id}.jpg"
                            cache_path.unlink(missing_ok=True)  # clear old custom cover
                            urllib.request.urlretrieve(cover_url, cache_path)
                            if cache_path.stat().st_size < 1000:
                                cache_path.unlink(missing_ok=True)
                        except Exception as e:
                            log.warning(f"Cover download failed: {e}")
                    metadata[book_id] = meta
                    save_metadata(metadata)
                    # Invalidate book cache so changes show immediately
                    BOOKS_CACHE["data"] = None
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception as e:
                    log.exception("set-meta error")
                    self.send_error(400)
                return

            self.send_error(404)
        except Exception as e:
            log.exception("POST error")
            self.send_error(500)

    def stream_file(self, file_path: Path, as_download: bool = False):
        size = file_path.stat().st_size
        range_header = self.headers.get("Range")
        ext = file_path.suffix.lower()
        if ext in ('.m4b', '.m4a', '.mp4'):
            mime = 'audio/mp4'
        elif ext == '.mp3':
            mime = 'audio/mpeg'
        else:
            mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

        extra = ""
        if as_download:
            safe_name = file_path.name.replace('"', '').replace("'", "")
            extra = f'attachment; filename="{safe_name}"'

        def write_range(start, end):
            if start < 0:
                start = 0
            if end >= size:
                end = size - 1
            if start > end or start >= size:
                self.send_error(416)
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            if extra:
                self.send_header("Content-Disposition", extra)
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)

        # Playback with no Range: send the whole file (this is what used to work).
        if not range_header or as_download:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            if extra:
                self.send_header("Content-Disposition", extra)
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
            return

        try:
            spec = range_header.split("=", 1)[1].split(",")[0].strip()
            if spec.startswith("-"):
                # bytes=-N means the last N bytes (needed for m4b headers)
                n = int(spec[1:] or "0")
                start = max(0, size - n)
                end = size - 1
            else:
                start_s, end_s = spec.split("-", 1)
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
        except Exception:
            start, end = 0, size - 1
        write_range(start, end)

    def log_error(self, format, *args):
        # send_error already records the status through log_request.
        return

    def log_message(self, format, *args):
        try:
            message = format % args
        except Exception:
            message = str(format)
        path = urlparse(self.path).path
        status = None
        if len(args) >= 2:
            try:
                status = int(args[1])
            except (TypeError, ValueError):
                status = None
        quiet = (
            path.startswith("/cover/")
            or path.startswith("/rest/getCoverArt")
            or path.startswith("/rest/stream")
            or path.startswith("/rest/download")
            or path.startswith("/rest/ping")
            or path.startswith("/rest/scrobble")
            or path == "/api/progress"
        )
        if path.startswith("/stream/") and self.headers.get("Range"):
            quiet = True
        if status is not None and status >= 500:
            quiet = False
        log.log(
            logging.DEBUG if quiet else logging.INFO,
            "%s %s",
            self.address_string(),
            redact(message),
        )

LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login · Audiobooks</title>
<style>
  body { 
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; 
    margin: 0; 
    min-height: 100vh; 
    display: flex; 
    align-items: center; 
    justify-content: center;
    background: #0d0d0f; 
    color: #f0f0f2; 
  }
  .card { 
    background: #18181b; 
    padding: 2.25rem; 
    border-radius: 16px; 
    width: 100%; 
    max-width: 380px; 
    box-shadow: 0 12px 40px rgba(0,0,0,0.4);
    border: 1px solid #2a2a2e;
  }
  h1 { margin: 0 0 1.5rem; text-align: center; font-weight: 600; letter-spacing: -0.02em; }
  input { 
    width: 100%; 
    padding: 0.75rem 0.85rem; 
    margin: 0.5rem 0; 
    border: 1px solid #2a2a2e; 
    border-radius: 8px; 
    background: #0d0d0f; 
    color: #f0f0f2; 
    box-sizing: border-box; 
    font-size: 1rem;
  }
  input:focus { outline: none; border-color: #5b8def; }
  button { 
    width: 100%; 
    padding: 0.75rem; 
    margin-top: 1rem; 
    background: #5b8def; 
    color: white; 
    border: none; 
    border-radius: 8px; 
    font-size: 1rem; 
    cursor: pointer; 
    font-weight: 500;
  }
  button:hover { background: #4a7de0; }
</style>
</head>
<body>
<div class="card">
  <h1>Audiobooks</h1>
  <!--ERROR-->
  <form method="POST" action="/login">
    <input name="username" placeholder="Username" required autofocus>
    <input name="password" type="password" placeholder="Password" required>
    <button type="submit">Login</button>
  </form>
</div>
</body>
</html>'''

HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audiobooks</title>
<style>
  :root { 
    --bg: #0d0d0f; 
    --fg: #f0f0f2; 
    --card: #18181b; 
    --card-hover: #222226;
    --accent: #5b8def; 
    --accent-hover: #4a7de0;
    --border: #2a2a2e; 
    --muted: #9a9aa3;
  }
  body.light { 
    --bg: #f7f7f8; 
    --fg: #1a1a1c; 
    --card: #ffffff; 
    --card-hover: #f0f0f2;
    --accent: #3b6fd8; 
    --accent-hover: #2f5bc4;
    --border: #e4e4e7; 
    --muted: #6b6b76;
  }
  body { 
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; 
    margin: 0; 
    background: var(--bg); 
    color: var(--fg); 
    line-height: 1.5;
  }
  header { 
    padding: 1rem 1.75rem; 
    background: var(--card); 
    border-bottom: 1px solid var(--border); 
    display: flex; 
    justify-content: space-between; 
    align-items: center; 
    position: sticky; 
    top: 0; 
    z-index: 10;
    backdrop-filter: blur(8px);
  }
  .header-left { display: flex; align-items: center; gap: 1.25rem; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 1.3rem; font-weight: 600; letter-spacing: -0.02em; }
  .tabs { display: flex; gap: 0.35rem; margin: 0 0 1rem; }
  .tabs button { flex: 1; font-weight: 600; }
  .tabs button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  #breadcrumb { font-size: 0.9rem; color: var(--muted); }
  #breadcrumb a { color: var(--accent); text-decoration: none; }
  #breadcrumb a:hover { text-decoration: underline; }
  #global-search { 
    padding: 0.45rem 0.75rem; 
    border-radius: 8px; 
    border: 1px solid var(--border); 
    background: var(--bg); 
    color: var(--fg); 
    width: 200px;
    font-size: 0.9rem;
  }
  #global-search:focus { outline: none; border-color: var(--accent); }
  #list { 
    padding: 1.75rem; 
    display: grid; 
    grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); 
    gap: 1.25rem; 
  }
  .item { 
    background: var(--card); 
    border-radius: 12px; 
    overflow: hidden; 
    cursor: pointer; 
    transition: transform 0.15s, box-shadow 0.15s, background 0.15s; 
    border: 1px solid var(--border);
  }
  .item:hover { 
    transform: translateY(-3px); 
    background: var(--card-hover);
    box-shadow: 0 8px 24px rgba(0,0,0,0.15);
  }
  .cover { 
    width: 100%; 
    aspect-ratio: 1; 
    object-fit: cover; 
    background: #222; 
    display: block; 
  }
  .folder-icon { 
    width: 100%; 
    aspect-ratio: 1; 
    background: linear-gradient(145deg, #2a2a30, #1f1f24); 
    display: flex; 
    align-items: center; 
    justify-content: center; 
    font-size: 3.2rem; 
  }
  body.light .folder-icon { background: linear-gradient(145deg, #eeeef0, #e4e4e8); }
  .item-info { padding: 0.7rem 0.75rem 0.85rem; }
  .item-name { font-weight: 550; font-size: 0.92rem; line-height: 1.35; }
  .author { font-size: 0.78rem; color: var(--muted); margin-top: 0.15rem; }
  button { 
    background: transparent; 
    color: var(--fg); 
    border: 1px solid var(--border); 
    padding: 0.4rem 0.75rem; 
    border-radius: 8px; 
    cursor: pointer; 
    font-size: 0.85rem;
    transition: background 0.15s, border-color 0.15s;
  }
  button:hover { 
    background: var(--accent); 
    color: #fff; 
    border-color: var(--accent); 
  }
  #continue { padding: 0.85rem 1.25rem 0; }
  #continue h3 { margin: 0 0 0.6rem; font-size: 0.95rem; font-weight: 600; color: var(--muted); }
  #continue-list {
    display: flex;
    gap: 0.75rem;
    overflow-x: auto;
    padding-bottom: 0.55rem;
    scroll-snap-type: x proximity;
  }
  #continue-list .item { flex: 0 0 156px; width: 156px; min-width: 0; scroll-snap-align: start; }
  .error { color: #f66; padding: 1rem; }
  #shell { display: flex; flex-direction: column; min-height: 100vh; }
  #sidebar {
    display: none;
    position: absolute;
    z-index: 40;
    top: 3.4rem;
    left: 0.75rem;
    width: 240px;
    max-width: calc(100vw - 1.5rem);
    max-height: calc(100vh - 4.5rem);
    overflow: auto;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 12px 32px rgba(0,0,0,.45);
    padding: 0.9rem 0.85rem;
  }
  #sidebar.open { display: block; }
  #main { width: 100%; max-width: none; margin: 0; display: flex; flex-direction: column; min-width: 0; }
  #main > * { min-width: 0; max-width: 100%; }
  #topbar {
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    padding: 0.6rem 1.25rem;
    background: var(--card);
    border-bottom: 1px solid var(--border);
  }
  #menu-btn { min-height: 40px; }
  #topbar #global-search { flex: 1; width: auto; max-width: none; min-height: 40px; font-size: 16px; }
  #breadcrumb { font-size: 0.85rem; padding: 0.75rem 1.25rem 0; }
  #list {
    padding: 0.75rem 1.25rem 1.5rem;
    grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
    gap: 0.85rem;
  }
  #list > p, #list > .error { grid-column: 1 / -1; margin: 0.25rem 0; }
  .panel { padding: 0.35rem 0.15rem 1.5rem; }
  .panel h3 { margin: 0.2rem 0 0.6rem; }
  .panel p { max-width: 70rem; }
  .panel-row { margin: 0.45rem 0; padding: 0.7rem 0.85rem; background: var(--card); border: 1px solid var(--border); border-radius: 8px; cursor: pointer; }
  .panel-row:hover { background: var(--card-hover); }
  @media (max-width: 700px) {
    #topbar { padding: 0.6rem 0.75rem; }
    #breadcrumb { padding: 0.7rem 0.75rem 0; }
    #continue { padding: 0.75rem 0.75rem 0; }
    #list { padding: 0.65rem 0.75rem 1.25rem; grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); gap: 0.65rem; }
    #continue-list .item { flex-basis: calc((100% - 0.75rem) / 2.15); width: calc((100% - 0.75rem) / 2.15); }
  }
</style>
</head>
<body>
<div id="shell">
  <aside id="sidebar">
    <h1 style="margin:0 0 0.8rem; font-size:1.15rem;">Library</h1>
    <div class="tabs">
      <button id="tab-books" type="button" onclick="setLibrary('books')">Audiobooks</button>
      <button id="tab-music" type="button" onclick="setLibrary('music')">Music</button>
    </div>
    <div style="display:flex; flex-direction:column; gap:0.4rem;">
      <button id="nav-authors" onclick="showAuthors()" style="text-align:left;">Authors</button>
      <button id="nav-series" onclick="showSeries()" style="text-align:left;">Series</button>
      <button id="nav-collections" onclick="showCollections()" style="text-align:left;">Collections</button>
      <button id="nav-finished" onclick="showFinished()" style="text-align:left;">Finished</button>
      <button onclick="showPlaylists()" style="text-align:left;">Playlists</button>
      <button onclick="rescan()" style="text-align:left;">Rescan</button>
      <button onclick="clearCovers()" style="text-align:left;">Clear Covers</button>
      <button onclick="toggleTheme()" style="text-align:left;">Theme</button>
      <a href="/password"><button style="width:100%; text-align:left;">Password</button></a>
      <a id="libraries-link" href="#" onclick="showLibraries(); return false;" style="display:none"><button style="width:100%; text-align:left;">Libraries</button></a>
      <a id="users-link" href="/users" style="display:none"><button style="width:100%; text-align:left;">Users</button></a>
      <a href="/logout"><button style="width:100%; text-align:left;">Logout</button></a>
    </div>
  </aside>
  <div id="main">
    <header id="topbar">
      <button id="menu-btn" type="button" onclick="toggleMenu(event)">Menu</button>
      <input id="global-search" placeholder="Search books..." oninput="globalSearch()">
      <button id="view-toggle" onclick="toggleView()" style="white-space:nowrap;">Folders</button>
      <select id="genre-filter" onchange="showAllBooks()" style="padding:0.45rem 0.5rem; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--fg); max-width:46vw;">
        <option value="">All genres</option>
      </select>
    </header>
    <div id="breadcrumb"></div>
    <div id="continue" style="display:none;">
      <h3>Continue Listening</h3>
      <div id="continue-list"></div>
    </div>
    <div id="list">Loading...</div>
  </div>
</div>
<script>
const IS_ADMIN = {{IS_ADMIN}};
if (IS_ADMIN) {
  document.getElementById('users-link').style.display = 'inline';
  document.getElementById('libraries-link').style.display = 'inline';
}

function fmtTime(seconds) {
  var s = Math.floor(Math.max(0, Number(seconds) || 0));
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  var ss = String(sec).padStart(2, '0');
  if (h) return h + ':' + String(m).padStart(2, '0') + ':' + ss;
  return m + ':' + ss;
}

function useGrid() {
  var list = document.getElementById('list');
  list.style.display = 'grid';
  return list;
}

function usePage(html) {
  var list = document.getElementById('list');
  list.style.display = 'block';
  list.innerHTML = html;
}

let currentPath = '';
let allBooksCache = null;
let viewMode = 'books';

function toggleMenu(e) {
  if (e) e.stopPropagation();
  var el = document.getElementById('sidebar');
  if (el) el.classList.toggle('open');
}
document.addEventListener('click', function(e) {
  var el = document.getElementById('sidebar');
  var btn = document.getElementById('menu-btn');
  if (!el || !el.classList.contains('open')) return;
  if (btn && btn.contains(e.target)) return;
  if (el.contains(e.target)) {
    if (e.target.closest('button, a')) el.classList.remove('open');
    return;
  }
  el.classList.remove('open');
});
let finishedIds = new Set();
const requestedLibrary = new URLSearchParams(location.search).get('library');
let library = requestedLibrary === 'music' || requestedLibrary === 'books'
  ? requestedLibrary
  : (localStorage.getItem('library') === 'music' ? 'music' : 'books');
viewMode = library === 'music' ? 'folders' : 'books';

function setLibrary(next) {
  library = next === 'music' ? 'music' : 'books';
  localStorage.setItem('library', library);
  allBooksCache = null;
  currentPath = '';
  viewMode = next === 'music' ? 'folders' : 'books';
  const search = document.getElementById('global-search');
  if (search) search.value = '';
  updateLibraryChrome();
  if (viewMode === 'books') showAllBooks();
  else load('');
}

function updateLibraryChrome() {
  const music = library === 'music';
  document.getElementById('tab-books').classList.toggle('active', !music);
  document.getElementById('tab-music').classList.toggle('active', music);
  document.getElementById('nav-collections').style.display = music ? 'none' : '';
  document.getElementById('nav-finished').style.display = music ? 'none' : '';
  document.getElementById('nav-authors').style.display = music ? 'none' : '';
  document.getElementById('nav-series').style.display = music ? 'none' : '';
  const filter = document.getElementById('genre-filter');
  if (filter) filter.style.display = music ? 'none' : '';
  document.getElementById('global-search').placeholder = music ? 'Search music...' : 'Search books...';
  const toggle = document.getElementById('view-toggle');
  if (viewMode === 'folders') toggle.textContent = music ? 'All Albums' : 'All Books';
  else toggle.textContent = 'Folders';
}

async function loadFinished() {
  try {
    const r = await fetch('/api/finished');
    finishedIds = new Set(await r.json());
  } catch (e) {
    finishedIds = new Set();
  }
}

function bookCard(b, extra) {
  return `
    <div class="item" onclick="location.href='/book/${b.id}'">
      <img class="cover" src="/cover/${b.id}" alt="" loading="lazy" onerror="this.style.background='#333'; this.src=''">
      <div class="item-info">
        <div class="item-name">${b.name || 'Untitled'}</div>
        ${b.author ? `<div class="author">${b.author}</div>` : ''}
        ${b.series ? `<div class="author">${esc(b.series)}${b.seriesIndex != null && b.seriesIndex !== '' ? ' #' + b.seriesIndex : ''}</div>` : ''}
        ${b.genre ? `<div class="author">${b.genre}</div>` : ''}
        ${extra || ''}
      </div>
    </div>`;
}

function toggleView() {
  viewMode = viewMode === 'folders' ? 'books' : 'folders';
  updateLibraryChrome();
  if (viewMode === 'books') {
    showAllBooks();
  } else {
    load('');
  }
}

async function showAllBooks() {
  const books = (await loadAllBooks()).filter(b => !finishedIds.has(b.id));
  const music = library === 'music';
  const filter = document.getElementById('genre-filter');
  if (filter) filter.style.display = music ? 'none' : '';
  document.getElementById('breadcrumb').innerHTML = music ? 'All Albums' : 'All Books';
  document.getElementById('continue').style.display = '';
  showContinue();
  const empty = music ? '<p>No music found.</p>' : '<p>No books found.</p>';
  useGrid().innerHTML = music
    ? (books.length ? books.map(b => bookCard(b)).join('') : empty)
    : renderGenreBooks(books);
}

function fillGenreFilter(books) {
  const sel = document.getElementById('genre-filter');
  if (!sel) return;
  const current = sel.value;
  const names = [];
  books.forEach(b => {
    const list = (b.genres && b.genres.length) ? b.genres : (b.genre ? [b.genre] : []);
    list.forEach(name => { if (name && names.indexOf(name) < 0) names.push(name); });
  });
  names.sort((a, b) => a.localeCompare(b));
  sel.innerHTML = '<option value="">All genres</option>' + names.map(name => {
    const safe = String(name).replace(/&/g, '&').replace(/</g, '<').replace(/"/g, '"');
    return '<option value="' + safe + '">' + safe + '</option>';
  }).join('');
  if ([...sel.options].some(o => o.value === current)) sel.value = current;
}

function booksByGenre(books) {
  const sel = document.getElementById('genre-filter');
  const genre = sel ? sel.value : '';
  let list = books.slice();
  if (genre) list = list.filter(b => ((b.genres && b.genres.length) ? b.genres : (b.genre ? [b.genre] : [])).indexOf(genre) >= 0);
  list.sort((a, b) => {
    const ga = (a.genre || 'Other').toLowerCase();
    const gb = (b.genre || 'Other').toLowerCase();
    if (ga !== gb) return ga < gb ? -1 : 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  return { list, genre };
}

function renderGenreBooks(books) {
  fillGenreFilter(books);
  const picked = booksByGenre(books);
  if (!picked.list.length) return '<p>No books found.</p>';
  let html = '';
  let last = '';
  picked.list.forEach(b => {
    const name = b.genre || 'Other';
    if (!picked.genre && name !== last) {
      html += '<h3 style="grid-column:1/-1; margin:0.8rem 0 0; font-size:0.95rem; color:var(--muted);">' + String(name).replace(/&/g, '&').replace(/</g, '<') + '</h3>';
      last = name;
    }
    html += bookCard(b);
  });
  return html;
}

async function loadAllBooks() {
  if (!allBooksCache) {
    const r = await fetch('/api/books?library=' + encodeURIComponent(library));
    allBooksCache = await r.json();
  }
  return allBooksCache;
}

function seriesSort(a, b) {
  const as = a.series || '\uffff';
  const bs = b.series || '\uffff';
  if (as !== bs) return as < bs ? -1 : 1;
  const ai = (a.seriesIndex == null || a.seriesIndex === '') ? 1e9 : Number(a.seriesIndex);
  const bi = (b.seriesIndex == null || b.seriesIndex === '') ? 1e9 : Number(b.seriesIndex);
  if (ai !== bi) return ai - bi;
  return (a.name || '').localeCompare(b.name || '');
}

async function showAuthors() {
  const books = (await loadAllBooks()).filter(b => !finishedIds.has(b.id));
  const groups = {};
  books.forEach(b => {
    const name = b.author || 'Unknown';
    if (!groups[name]) groups[name] = [];
    groups[name].push(b);
  });
  const names = Object.keys(groups).sort((a, b) => a.localeCompare(b));
  window._authorNames = names;
  let html = '<div class="panel"><h3>Authors</h3>';
  if (!names.length) html += '<p>No authors yet.</p>';
  names.forEach((name, i) => {
    html += '<div class="panel-row" onclick="showAuthor(' + i + ')"><strong>' + esc(name) + '</strong> <span style="color:var(--muted);">(' + groups[name].length + ')</span></div>';
  });
  html += '</div>';
  usePage(html);
  document.getElementById('breadcrumb').innerHTML = 'Authors';
  document.getElementById('continue').style.display = 'none';
}

async function showAuthor(index) {
  const name = (window._authorNames || [])[index];
  if (!name) return;
  const books = (await loadAllBooks()).filter(b => !finishedIds.has(b.id) && (b.author || 'Unknown') === name);
  books.sort(seriesSort);
  document.getElementById('breadcrumb').innerHTML = '<a href="#" onclick="showAuthors(); return false;">Authors</a> / ' + esc(name);
  document.getElementById('continue').style.display = 'none';
  useGrid().innerHTML = books.length ? books.map(b => bookCard(b)).join('') : '<p>No books.</p>';
}

async function showSeries() {
  const books = (await loadAllBooks()).filter(b => !finishedIds.has(b.id) && b.series);
  const groups = {};
  books.forEach(b => {
    if (!groups[b.series]) groups[b.series] = [];
    groups[b.series].push(b);
  });
  const names = Object.keys(groups).sort((a, b) => a.localeCompare(b));
  window._seriesNames = names;
  let html = '<div class="panel"><h3>Series</h3>';
  if (!names.length) html += '<p>No series yet. Series come from an Audible sidecar, audio tags, or a numbered folder.</p>';
  names.forEach((name, i) => {
    const count = groups[name].length;
    html += '<div class="panel-row" onclick="showOneSeries(' + i + ')"><strong>' + esc(name) + '</strong> <span style="color:var(--muted);">(' + count + (count === 1 ? ' book' : ' books') + ')</span></div>';
  });
  html += '</div>';
  usePage(html);
  document.getElementById('breadcrumb').innerHTML = 'Series';
  document.getElementById('continue').style.display = 'none';
}

async function showOneSeries(index) {
  const name = (window._seriesNames || [])[index];
  if (!name) return;
  const books = (await loadAllBooks()).filter(b => !finishedIds.has(b.id) && b.series === name);
  books.sort(seriesSort);
  document.getElementById('breadcrumb').innerHTML = '<a href="#" onclick="showSeries(); return false;">Series</a> / ' + esc(name);
  document.getElementById('continue').style.display = 'none';
  useGrid().innerHTML = books.length ? books.map(b => bookCard(b)).join('') : '<p>No books.</p>';
}

async function showCollections() {
  const r = await fetch('/api/collections');
  const cols = await r.json();
  const names = Object.keys(cols);
  let html = '<div class="panel"><h3>Collections</h3>';
  html += '<div style="margin:1rem 0;"><input id="new-col" placeholder="New collection name" style="padding:0.4rem; margin-right:0.5rem;"><button onclick="createCollection()">Create</button></div>';
  if (!names.length) html += '<p>No collections yet.</p>';
  else {
    html += names.map(n => `
      <div style="margin:0.5rem 0; padding:0.6rem; background:var(--card); border-radius:8px; cursor:pointer;" onclick="viewCollection('${n.replace(/'/g, "\\'")}')">
        <strong>${n}</strong> <span style="color:var(--muted);">(${cols[n].length} books)</span>
        <button onclick="event.stopPropagation(); deleteCollection('${n.replace(/'/g, "\\'")}')" style="float:right; padding:0.2rem 0.5rem;">Delete</button>
      </div>`).join('');
  }
  html += '</div>';
  usePage(html);
  document.getElementById('breadcrumb').innerHTML = 'Collections';
}

async function createCollection() {
  const name = document.getElementById('new-col').value.trim();
  if (!name) return;
  await fetch('/api/collections', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'create', name}) });
  showCollections();
}

async function deleteCollection(name) {
  if (!confirm('Delete collection "' + name + '"?')) return;
  await fetch('/api/collections', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'delete', name}) });
  showCollections();
}

async function viewCollection(name) {
  const [colsRes, books] = await Promise.all([fetch('/api/collections').then(r=>r.json()), loadAllBooks()]);
  const ids = colsRes[name] || [];
  const booksInCol = books.filter(b => ids.includes(b.id));
  document.getElementById('breadcrumb').innerHTML = 'Collections / ' + name;
  useGrid().innerHTML = booksInCol.length ? booksInCol.map(b => bookCard(b)).join('') : '<p>Empty collection.</p>';
}

async function showPlaylists() {
  const r = await fetch('/api/playlists');
  const lists = await r.json();
  let html = '<div class="panel"><h3>Playlists</h3>';
  html += '<p style="color:var(--muted);">Yours only, unless you mark one public. Other people can play a public playlist but cannot change it.</p>';
  html += '<div style="margin:1rem 0; display:flex; gap:0.5rem; max-width:36rem;"><input id="new-pl" placeholder="New playlist name" style="flex:1; padding:0.45rem 0.6rem; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--fg);"><button onclick="createPlaylist()">Create</button></div>';
  if (!lists.length) html += '<p>No playlists yet.</p>';
  lists.forEach(pl => {
    const label = esc(pl.name) + ' <span style="color:var(--muted);">(' + pl.songCount + ')</span>';
    const who = pl.mine ? (pl.public ? 'public' : 'private') : ('by ' + esc(pl.owner));
    html += '<div style="margin:0.5rem 0; padding:0.7rem; background:var(--card); border:1px solid var(--border); border-radius:8px; cursor:pointer;" onclick="location.href=\\'/playlist/' + pl.id + '\\'">';
    html += '<strong>' + label + '</strong> <span style="color:var(--muted);">' + who + '</span>';
    if (pl.mine) html += '<button onclick="event.stopPropagation(); deletePlaylist(\\'' + pl.id + '\\')" style="float:right;">Delete</button>';
    html += '</div>';
  });
  html += '</div>';
  usePage(html);
  document.getElementById('breadcrumb').innerHTML = 'Playlists';
  document.getElementById('continue').style.display = 'none';
}

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, function(c) {
    if (c === '&') return '&' + 'amp;';
    if (c === '<') return '&' + 'lt;';
    if (c === '>') return '&' + 'gt;';
    if (c === String.fromCharCode(34)) return '&' + 'quot;';
    return '&' + '#39;';
  });
}

async function createPlaylist() {
  const name = document.getElementById('new-pl').value.trim();
  if (!name) return;
  const r = await fetch('/api/playlists', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'create', name})
  });
  if (!r.ok) { alert(await r.text()); return; }
  const pl = await r.json();
  location.href = '/playlist/' + pl.id;
}

async function deletePlaylist(id) {
  if (!confirm('Delete this playlist?')) return;
  const r = await fetch('/api/playlists', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'delete', id})
  });
  if (!r.ok) { alert(await r.text()); return; }
  showPlaylists();
}

async function showFinished() {
  await loadFinished();
  const books = await loadAllBooks();
  const done = books.filter(b => finishedIds.has(b.id));
  document.getElementById('breadcrumb').innerHTML = '<a href="#" onclick="load(\\'\\'); return false;">Library</a> / Finished';
  document.getElementById('continue').style.display = 'none';
  useGrid().innerHTML = done.length ? done.map(b => bookCard(b,
    `<button onclick="event.stopPropagation(); returnToLibrary('${b.id}')" style="margin-top:0.4rem;">Return to library</button>`
  )).join('') : '<p>No finished books. Open a book and choose Mark Finished.</p>';
}

async function returnToLibrary(id) {
  await fetch('/api/finished', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id, action: 'remove' })
  });
  showFinished();
}

async function showContinue() {
  try {
    const [books, progRes] = await Promise.all([loadAllBooks(), fetch('/api/progress')]);
    const prog = await progRes.json();
    const recent = Object.entries(prog)
      .map(([id, p]) => ({ id, ...p, book: books.find(b => b.id === id) }))
      .filter(x => x.book && x.time > 10 && !finishedIds.has(x.id))
      .sort((a, b) => (b.updated || 0) - (a.updated || 0))
      .slice(0, 8);
    const el = document.getElementById('continue');
    if (!recent.length) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    document.getElementById('continue-list').innerHTML = recent.map(x => {
      const folder = x.book.path ? x.book.path.split('/').slice(-2, -1)[0] || x.book.path : '';
      return `
      <div class="item" style="position:relative;" onclick="location.href='/book/${x.id}'">
        <img class="cover" src="/cover/${x.id}" alt="" loading="lazy" onerror="this.style.background='#333'; this.src=''">
        <div class="item-info">
          <div class="item-name">${x.book.name || 'Untitled'}</div>
          <div class="author">${fmtTime(x.time)}</div>
          ${folder ? `<div class="author">${folder}</div>` : ''}
        </div>
        <button onclick="event.stopPropagation(); removeContinue('${x.id}')" style="position:absolute; top:4px; right:4px; padding:0.15rem 0.4rem; font-size:0.75rem; background:rgba(0,0,0,.55); color:#fff; border:none; border-radius:6px;">×</button>
      </div>`;
    }).join('');
  } catch (e) {}
}

async function removeContinue(id) {
  await fetch('/api/clear-progress?id=' + encodeURIComponent(id));
  showContinue();
}

async function globalSearch() {
  const q = document.getElementById('global-search').value.toLowerCase().trim();
  if (!q) { load(currentPath); return; }
  const books = await loadAllBooks();
  const matches = books.filter(b =>
    !finishedIds.has(b.id) && (
      (b.name || '').toLowerCase().includes(q) ||
      (b.author || '').toLowerCase().includes(q) ||
      (b.narrator || '').toLowerCase().includes(q) ||
      (b.genre || '').toLowerCase().includes(q) ||
      (b.series || '').toLowerCase().includes(q) ||
      ((b.genres || []).join(' ').toLowerCase().includes(q)) ||
      (b.path || '').toLowerCase().includes(q)
    )
  );
  const empty = library === 'music' ? '<p>No music matches</p>' : '<p>No matches</p>';
  useGrid().innerHTML = matches.length ? matches.map(b => bookCard(b)).join('') : empty;
  document.getElementById('breadcrumb').innerHTML = 'Search results';
}

function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('theme', document.body.classList.contains('light') ? 'light' : 'dark');
}
if (localStorage.getItem('theme') === 'light') document.body.classList.add('light');

async function rescan() {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  try {
    await fetch('/api/rescan');
    allBooksCache = null;
    load(currentPath);
  } catch (e) {
    alert('Rescan failed');
  }
  btn.disabled = false;
  btn.textContent = 'Rescan';
}

async function clearCovers() {
  if (!confirm('Clear cached covers for the current folder? They will be re-fetched from Open Library.')) return;
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Clearing...';
  try {
    const r = await fetch('/api/clear-covers?path=' + encodeURIComponent(currentPath));
    const data = await r.json();
    alert('Cleared ' + (data.cleared || 0) + ' cover(s)');
    load(currentPath);
  } catch (e) {
    alert('Failed to clear covers');
  }
  btn.disabled = false;
  btn.textContent = 'Clear Covers';
}

async function load(path = '') {
  currentPath = path;
  try {
    const r = await fetch('/api/browse?library=' + encodeURIComponent(library) + '&path=' + encodeURIComponent(path));
    if (!r.ok) throw new Error('Failed to load');
    const data = await r.json();
    render(data);
    updateBreadcrumb(path, data.rootName);
    showContinue();
  } catch (e) {
    document.getElementById('list').innerHTML = '<div class="error">' + e.message + '</div>';
  }
}

async function showLibraries() {
  const r = await fetch('/api/libraries');
  const libs = await r.json();
  window._libs = libs;
  let html = '<div class="panel"><h3>Library folders</h3>';
  html += '<p style="color:var(--muted);">The first folder is the original library and stays. Add other full paths on this server. Files are not moved or deleted.</p>';
  html += '<div style="margin:1rem 0; display:flex; gap:0.5rem; flex-wrap:wrap;">';
  html += '<input id="new-lib" placeholder="/home/you/files/more-books" style="flex:1; min-width:240px; padding:0.45rem 0.6rem; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--fg);">';
  html += '<button onclick="addLibrary()">Add</button></div>';
  html += '<div id="lib-msg" style="min-height:1.2rem; color:#f66;"></div>';
  libs.forEach((lib, i) => {
    html += '<div style="margin:0.5rem 0; padding:0.7rem; background:var(--card); border:1px solid var(--border); border-radius:8px;">';
    html += '<strong>' + lib.name + '</strong>';
    html += '<div style="color:var(--muted); font-size:0.85rem; word-break:break-all;">' + lib.path + '</div>';
    if (!lib.exists) html += '<div style="color:#f66;">Folder not found</div>';
    if (lib.removable) html += '<button onclick="removeLibrary(' + i + ')" style="margin-top:0.4rem;">Remove</button>';
    else html += '<div style="color:var(--muted); font-size:0.8rem; margin-top:0.25rem;">Original library</div>';
    html += '</div>';
  });
  html += '</div>';
  usePage(html);
  document.getElementById('breadcrumb').innerHTML = 'Libraries';
  document.getElementById('continue').style.display = 'none';
}

async function addLibrary() {
  const path = document.getElementById('new-lib').value.trim();
  const msg = document.getElementById('lib-msg');
  const r = await fetch('/api/libraries', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action: 'add', path })
  });
  if (!r.ok) {
    msg.textContent = await r.text();
    return;
  }
  allBooksCache = null;
  showLibraries();
}

async function removeLibrary(i) {
  const lib = (window._libs || [])[i];
  if (!lib) return;
  if (!confirm('Remove this folder from the library? The files stay on disk.')) return;
  const r = await fetch('/api/libraries', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ action: 'remove', path: lib.path })
  });
  if (!r.ok) {
    alert(await r.text());
    return;
  }
  allBooksCache = null;
  showLibraries();
}

function updateBreadcrumb(path, rootName) {
  const rootLabel = library === 'music' ? 'Music' : 'Audiobooks';
  let shown = path || '';
  let accum = '';
  if (library === 'music') {
    const rootMatch = shown.match(/^(~\\d+~)\\/?/);
    if (rootMatch) {
      accum = rootMatch[1];
      shown = shown.slice(rootMatch[0].length);
    }
  }
  const parts = shown ? shown.split('/').filter(Boolean) : [];
  let html = '<a href="#" onclick="load(\\'\\'); return false;">' + rootLabel + '</a>';
  parts.forEach(p => {
    accum = accum ? accum + '/' + p : p;
    const label = (/^~\\d+~$/.test(p) && rootName) ? rootName : p;
    html += ' / <a href="#" onclick="load(\\'' + accum.replace(/'/g, "\\\\'") + '\\'); return false;">' + label + '</a>';
  });
  document.getElementById('breadcrumb').innerHTML = html;
}

function render(data) {
  const list = document.getElementById('list');
  list.style.display = 'grid';
  if (data.missing) {
    list.style.display = 'block';
    list.innerHTML = '<p style="padding:0 0.25rem;">No music folder yet. Create ~/files/music, or set MUSIC_DIR, then choose Rescan.</p>';
    return;
  }
  let html = '';

  data.folders.forEach(f => {
    html += `
      <div class="item" onclick="load('${f.path.replace(/'/g, "\\\\'")}')">
        <div class="folder-icon">📁</div>
        <div class="item-info"><div class="item-name">${f.name}</div></div>
      </div>`;
  });

  data.books.filter(b => !finishedIds.has(b.id)).forEach(b => {
    html += bookCard(b);
  });

  const empty = library === 'music' ? '<p>No music in this folder.</p>' : '<p>Empty folder</p>';
  list.innerHTML = html || empty;
}

updateLibraryChrome();
loadFinished().then(function() {
  updateLibraryChrome();
  if (library === 'books') showAllBooks();
  else { load(''); showContinue(); }
});
</script>
</body>
</html>'''

PLAYLIST_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Playlist</title>
<style>
  :root { --bg:#0d0d0f; --fg:#f0f0f2; --card:#18181b; --accent:#5b8def; --border:#2a2a2e; --muted:#9a9aa3; }
  body.light { --bg:#f7f7f8; --fg:#1a1a1c; --card:#fff; --accent:#3b6fd8; --border:#e4e4e7; --muted:#6b6b76; }
  body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; margin:0; background:var(--bg); color:var(--fg); }
  header { padding:0.9rem 1.25rem; background:var(--card); border-bottom:1px solid var(--border); }
  header a { color:var(--accent); text-decoration:none; }
  .wrap { margin:0; padding:1.25rem 1.5rem 2rem; }
  @media (max-width: 700px) { .wrap { padding: 0.9rem 0.75rem 1.5rem; } }
  button { background:transparent; color:var(--fg); border:1px solid var(--border); padding:0.35rem 0.65rem; border-radius:8px; cursor:pointer; }
  button:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
  li { list-style:none; margin:0.35rem 0; padding:0.55rem 0.7rem; background:var(--card); border:1px solid var(--border); border-radius:8px; display:flex; gap:0.5rem; align-items:center; }
  li.active { border-color:var(--accent); }
  .grow { flex:1; cursor:pointer; }
  .muted { color:var(--muted); font-size:0.85rem; }
</style>
</head>
<body>
<header><a href="/">&larr; Library</a></header>
<div class="wrap">
  <h1 id="title">Loading...</h1>
  <div id="owner" class="muted"></div>
  <div id="tools" style="margin:0.8rem 0; display:flex; gap:0.4rem; flex-wrap:wrap;"></div>
  <audio id="audio" controls style="width:100%; margin:0.5rem 0 1rem;"></audio>
  <ul id="tracks" style="padding:0;"></ul>
</div>
<script>
const PID = "{{PLAYLIST_ID}}";
let pl = null;
let tracks = [];
let current = 0;
const audio = document.getElementById("audio");
if (localStorage.getItem("theme") === "light") document.body.classList.add("light");

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, function(c) {
    if (c === "&") return "&" + "amp;";
    if (c === "<") return "&" + "lt;";
    if (c === ">") return "&" + "gt;";
    if (c === String.fromCharCode(34)) return "&" + "quot;";
    return "&" + "#39;";
  });
}

function fmtTime(seconds) {
  var s = Math.floor(Math.max(0, Number(seconds) || 0));
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  var ss = String(sec).padStart(2, "0");
  if (h) return h + ":" + String(m).padStart(2, "0") + ":" + ss;
  return m + ":" + ss;
}

async function load() {
  const r = await fetch("/api/playlists?id=" + encodeURIComponent(PID));
  if (!r.ok) {
    document.getElementById("title").textContent = "Playlist not found";
    return;
  }
  pl = await r.json();
  tracks = pl.songs || [];
  document.getElementById("title").textContent = pl.name || "Playlist";
  document.title = pl.name || "Playlist";
  document.getElementById("owner").textContent = pl.mine ? (pl.public ? "Public" : "Private") : ("by " + (pl.owner || ""));
  const tools = document.getElementById("tools");
  if (pl.mine) {
    tools.innerHTML = '<button onclick="renamePlaylist()">Rename</button><button onclick="togglePublic()"></button><button onclick="removePlaylist()">Delete</button>';
    tools.children[1].textContent = pl.public ? "Make private" : "Make public";
  } else {
    tools.innerHTML = "";
  }
  render();
}

function render() {
  const ul = document.getElementById("tracks");
  if (!tracks.length) {
    ul.innerHTML = '<p class="muted">Empty playlist. Open an album or book and use Add to playlist.</p>';
    return;
  }
  ul.innerHTML = tracks.map(function(t, i) {
    const manage = pl.mine
      ? '<button onclick="moveTrack(' + i + ',-1)">Up</button><button onclick="moveTrack(' + i + ',1)">Down</button><button onclick="removeTrack(' + i + ')">Remove</button>'
      : "";
    const dur = t.duration ? '<span class="muted">' + fmtTime(t.duration) + '</span>' : '';
    return '<li class="' + (i === current ? "active" : "") + '"><div class="grow" onclick="play(' + i + ', true)"><div>' + esc(t.title) + '</div><div class="muted">' + esc(t.artist) + (t.album ? " · " + esc(t.album) : "") + '</div></div>' + dur + manage + "</li>";
  }).join("");
}

function play(i, andPlay) {
  if (i < 0 || i >= tracks.length) return;
  current = i;
  const t = tracks[i];
  audio.src = "/stream/" + encodeURIComponent(t.bookId) + "/" + t.fileIndex;
  render();
  if (andPlay) audio.play();
}

audio.addEventListener("ended", function() {
  if (current + 1 < tracks.length) play(current + 1, true);
});

async function post(body) {
  const r = await fetch("/api/playlists", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  if (!r.ok) { alert(await r.text()); return null; }
  return r.json();
}

async function renamePlaylist() {
  const name = prompt("Playlist name", pl.name || "");
  if (!name || !name.trim()) return;
  if (await post({action:"rename", id:PID, name:name.trim()})) load();
}

async function togglePublic() {
  if (await post({action:"public", id:PID, public:!pl.public})) load();
}

async function removePlaylist() {
  if (!confirm("Delete this playlist?")) return;
  const r = await fetch("/api/playlists", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action:"delete", id:PID})
  });
  if (!r.ok) { alert(await r.text()); return; }
  location.href = "/";
}

async function removeTrack(i) {
  if (await post({action:"remove", id:PID, index:i})) {
    if (current >= i && current > 0) current -= 1;
    load();
  }
}

async function moveTrack(i, direction) {
  if (await post({action:"move", id:PID, index:i, direction:direction})) load();
}

load();
</script>
</body>
</html>'''

BOOK_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Book</title>
<style>
  :root { 
    --bg: #0d0d0f; 
    --fg: #f0f0f2; 
    --card: #18181b; 
    --accent: #5b8def; 
    --border: #2a2a2e; 
    --muted: #9a9aa3;
  }
  body.light { 
    --bg: #f7f7f8; 
    --fg: #1a1a1c; 
    --card: #ffffff; 
    --accent: #3b6fd8; 
    --border: #e4e4e7; 
    --muted: #6b6b76;
  }
  body { 
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; 
    margin: 0; 
    background: var(--bg); 
    color: var(--fg); 
  }
  header { 
    padding: 1rem 1.75rem; 
    background: var(--card); 
    border-bottom: 1px solid var(--border); 
  }
  header a { color: var(--accent); text-decoration: none; font-size: 0.95rem; }
  header a:hover { text-decoration: underline; }
  .container { max-width: 860px; margin: 0 auto; padding: 2rem 1.5rem; }
  .top { display: flex; gap: 1.75rem; flex-wrap: wrap; margin-bottom: 1.75rem; }
  .cover { 
    width: 220px; 
    height: 220px; 
    object-fit: cover; 
    border-radius: 12px; 
    background: #222; 
    box-shadow: 0 8px 24px rgba(0,0,0,0.2);
  }
  .info h1 { margin: 0 0 0.3rem; font-size: 1.7rem; font-weight: 600; letter-spacing: -0.02em; }
  .author { color: var(--muted); margin-bottom: 1rem; font-size: 1rem; }
  #tracks { list-style: none; padding: 0; margin: 0; }
  #tracks li { 
    padding: 0.7rem 0.85rem; 
    border-bottom: 1px solid var(--border); 
    cursor: pointer; 
    border-radius: 6px;
    transition: background 0.12s;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;
  }
  .ch-title { flex: 1; min-width: 0; }
  .ch-time { margin-left: auto; text-align: right; flex: none; }
  .ch-len, .ch-start { display: block; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .ch-len { color: var(--fg); font-size: 0.92rem; }
  .ch-start { color: var(--muted); font-size: 0.75rem; }
  #tracks li button { flex: none; }
  #tracks li:hover, #tracks li.active { background: var(--card); }
  #player { 
    margin-top: 1.75rem; 
    background: var(--card); 
    padding: 1.25rem; 
    border-radius: 12px; 
    border: 1px solid var(--border);
  }
  audio { width: 100%; border-radius: 8px; }
  .controls { margin-top: 0.75rem; display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  button { 
    background: var(--accent); 
    color: #fff; 
    border: none; 
    padding: 0.45rem 0.85rem; 
    border-radius: 8px; 
    cursor: pointer; 
    font-size: 0.9rem;
  }
  button:hover { background: var(--accent-hover, #4a7de0); }
  #edit-meta input { 
    width: 100%; 
    margin: 0.35rem 0; 
    padding: 0.55rem 0.7rem; 
    background: var(--bg); 
    color: var(--fg); 
    border: 1px solid var(--border); 
    border-radius: 8px; 
    box-sizing: border-box;
  }
</style>
</head>
<body>
<header>
  <a href="/">&larr; Library</a>
</header>
<div class="container">
  <div class="top">
    <img id="cover" class="cover" alt="">
    <div class="info">
      <h1 id="title">Loading...</h1>
      <div class="author" id="author"></div>
      <div class="author" id="series"></div>
      <div id="series-next" style="margin:0.35rem 0 0.2rem;"></div>
      <div id="progress-info" style="margin-top:0.5rem; font-weight:500;"></div>
      <div style="margin-top:1rem;">
        <button onclick="document.getElementById('edit-meta').style.display='block'">Edit Info / Cover</button>
        <button onclick="addToCollection()" style="margin-left:0.5rem;">Add to Collection</button>
        <button onclick="addToPlaylist('album')" style="margin-left:0.5rem;">Add to playlist</button>
        <button onclick="addToPlaylist('track')" style="margin-left:0.5rem;">Add track</button>
        <button id="finish-btn" onclick="toggleFinished()" style="margin-left:0.5rem;">Mark Finished</button>
      </div>
      <div id="edit-meta" style="display:none; margin-top:1rem; background:var(--card); padding:1rem; border-radius:8px;">
        <input id="meta-title" placeholder="Title" style="width:100%; margin:0.3rem 0; padding:0.5rem; background:var(--bg); color:var(--fg); border:1px solid var(--border); border-radius:4px;">
        <input id="meta-author" placeholder="Author" style="width:100%; margin:0.3rem 0; padding:0.5rem; background:var(--bg); color:var(--fg); border:1px solid var(--border); border-radius:4px;">
        <input id="meta-cover" placeholder="ISBN, Open Library link, Google Books link, or direct cover URL" style="width:100%; margin:0.3rem 0; padding:0.5rem; background:var(--bg); color:var(--fg); border:1px solid var(--border); border-radius:4px;">
        <button onclick="saveMeta()">Save</button>
        <button onclick="document.getElementById('edit-meta').style.display='none'" style="background:transparent; border:1px solid var(--border);">Cancel</button>
        <div id="meta-msg" style="margin-top:0.5rem; font-size:0.9rem;"></div>
      </div>
    </div>
  </div>
  <ul id="tracks"></ul>
  <div id="player">
    <audio id="audio" controls preload="none" style="width:100%"></audio>
    <div class="controls">
      <button id="play-btn" type="button" onclick="togglePlay()">Play</button>
      <button type="button" onclick="skip(-30)">-30s</button>
      <button type="button" onclick="skip(-15)">-15s</button>
      <button type="button" onclick="prev()">Prev</button>
      <button type="button" onclick="next()">Next</button>
      <button type="button" onclick="skip(15)">+15s</button>
      <button type="button" onclick="skip(30)">+30s</button>
      <span id="track-label"></span>
    </div>
    <div class="controls">
      <label>Volume: <input type="range" id="volume" min="0" max="1" step="0.05" value="1" oninput="audio.volume=this.value" style="width:100px;"></label>
      <label>Speed:
        <select id="speed" onchange="setSpeed(this.value)">
          <option value="0.75">0.75×</option>
          <option value="1" selected>1×</option>
          <option value="1.25">1.25×</option>
          <option value="1.5">1.5×</option>
          <option value="2">2×</option>
        </select>
      </label>
      <label>Sleep:
        <select id="sleep" onchange="setSleep(this.value)">
          <option value="0">Off</option>
          <option value="15">15 min</option>
          <option value="30">30 min</option>
          <option value="60">60 min</option>
        </select>
      </label>
      <span id="time-info"></span>
    </div>
    <div style="margin-top:0.75rem;">
      <button onclick="addBookmark()">+ Bookmark</button>
      <a id="download-link" href="#" download style="margin-left:0.5rem;"><button>Download Track</button></a>
    </div>
    <div id="bookmarks" style="margin-top:0.75rem; font-size:0.9rem;"></div>
  </div>
</div>
<script>
const BOOK_ID = "{{BOOK_ID}}";
let book = null, fileIndex = 0, progress = {}, userPrefs = {}, bookBookmarks = [];
const audio = document.getElementById('audio');

function fmtTime(seconds) {
  var s = Math.floor(Math.max(0, Number(seconds) || 0));
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  var ss = String(sec).padStart(2, '0');
  if (h) return h + ':' + String(m).padStart(2, '0') + ':' + ss;
  return m + ':' + ss;
}

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, function(c) {
    if (c === '&') return '&' + 'amp;';
    if (c === '<') return '&' + 'lt;';
    if (c === '>') return '&' + 'gt;';
    if (c === '"') return '&' + 'quot;';
    return '&' + '#39;';
  });
}
audio.volume = 1;
audio.muted = false;
let nextAudio = new Audio();
let preloadedIndex = -1;
let sleepTimer = null;

function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('theme', document.body.classList.contains('light') ? 'light' : 'dark');
}
if (localStorage.getItem('theme') === 'light') document.body.classList.add('light');

async function load() {
  document.getElementById('title').textContent = 'Loading...';
  try {
    const bookRes = await fetch('/api/book/' + encodeURIComponent(BOOK_ID));
    if (!bookRes.ok) {
      document.getElementById('title').textContent = 'Book not found (' + bookRes.status + ')';
      return;
    }
    book = await bookRes.json();
    document.getElementById('title').textContent = book.name || 'Untitled';
    const who = book.author || '';
    document.getElementById('author').textContent = book.narrator ? (who + (who ? ' · ' : '') + 'Narrated by ' + book.narrator) : who;
    const seriesEl = document.getElementById('series');
    if (book.series) {
      seriesEl.textContent = book.series + ((book.seriesIndex != null && book.seriesIndex !== '') ? (' #' + book.seriesIndex) : '');
    } else if (seriesEl) {
      seriesEl.textContent = '';
    }
    const nextEl = document.getElementById('series-next');
    if (nextEl) {
      if (book.nextInSeries && book.nextInSeries.id) {
        const label = book.nextInSeries.name || 'Next';
        const num = (book.nextInSeries.seriesIndex != null && book.nextInSeries.seriesIndex !== '') ? (' #' + book.nextInSeries.seriesIndex) : '';
        nextEl.innerHTML = '<a href="/book/' + encodeURIComponent(book.nextInSeries.id) + '" style="color:var(--accent);">Next in series: ' + esc(label) + esc(num) + '</a>';
      } else {
        nextEl.innerHTML = '';
      }
    }
    if (book.kind === 'music') {
      const back = document.querySelector('header a');
      back.href = '/?library=music';
      back.innerHTML = '&larr; Music';
      document.title = book.name || 'Album';
      const finish = document.getElementById('finish-btn');
      if (finish) finish.style.display = 'none';
      const addCol = document.querySelector('button[onclick="addToCollection()"]');
      if (addCol) addCol.style.display = 'none';
    }
    document.getElementById('meta-title').value = book.name || '';
    document.getElementById('meta-author').value = book.author || '';
    document.getElementById('cover').src = '/cover/' + encodeURIComponent(BOOK_ID) + '?t=' + Date.now();
    renderTracks();

    // Load progress and prefs in the background
    try {
      const [progRes, prefsRes] = await Promise.all([
        fetch('/api/progress'),
        fetch('/api/prefs')
      ]);
      progress = await progRes.json();
      userPrefs = await prefsRes.json();
      const saved = progress[BOOK_ID] || {};
      fileIndex = saved.fileIndex || 0;
      const speed = userPrefs.speed || 1;
      document.getElementById('speed').value = speed;
      audio.playbackRate = parseFloat(speed);
      if (saved.time) {
        document.getElementById('progress-info').textContent = 'Resume at ' + fmtTime(saved.time);
      }
      updateActive();
    } catch (e) {
      console.error('Progress load failed', e);
    }
    loadBookmarks();
    refreshFinishBtn();
  } catch (e) {
    document.getElementById('title').textContent = 'Error loading book';
    console.error(e);
  }
}

async function loadBookmarks() {
  const r = await fetch('/api/bookmarks?id=' + BOOK_ID);
  bookBookmarks = await r.json();
  renderBookmarks();
}

function renderBookmarks() {
  const el = document.getElementById('bookmarks');
  if (!bookBookmarks.length) { el.innerHTML = ''; return; }
  const row = (bm, i) => `
      <li style="margin:0.3rem 0;">
        <a href="#" onclick="jumpTo(${bm.fileIndex}, ${bm.time}); return false;">
          Track ${bm.fileIndex+1} @ ${fmtTime(bm.time)}
        </a>
        ${bm.note && bm.note !== 'auto' ? ' — ' + bm.note : ''}
        <button onclick="deleteBookmark(${i})" style="margin-left:0.5rem; padding:0.1rem 0.4rem; font-size:0.8rem;">×</button>
      </li>`;
  const manual = [];
  const autos = [];
  bookBookmarks.forEach((bm, i) => {
    const isAuto = bm.manual ? false : (bm.auto || bm.note === 'auto');
    (isAuto ? autos : manual).push(row(bm, i));
  });
  let html = '';
  if (manual.length) {
    html += '<strong>Bookmarks</strong><div style="color:var(--muted);font-size:0.8rem;">Kept until you delete them.</div><ul style="list-style:none;padding:0;">' + manual.join('') + '</ul>';
  }
  if (autos.length) {
    html += '<strong style="display:block;margin-top:0.6rem;">Auto checkpoints</strong><div style="color:var(--muted);font-size:0.8rem;">Last 10. Older ones drop off on their own.</div><ul style="list-style:none;padding:0;">' + autos.join('') + '</ul>';
  }
  el.innerHTML = html;
}

async function addBookmark() {
  const note = prompt('Note (optional):') || '';
  await fetch('/api/bookmarks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: BOOK_ID, action: 'add', fileIndex, time: audio.currentTime, note })
  });
  loadBookmarks();
}

async function deleteBookmark(idx) {
  await fetch('/api/bookmarks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: BOOK_ID, action: 'delete', index: idx })
  });
  loadBookmarks();
}

function jumpTo(fi, t) {
  fileIndex = fi;
  updateActive();
  loadFile();
  setTimeout(() => { audio.currentTime = t; }, 300);
}

function setSpeed(v) {
  audio.playbackRate = parseFloat(v);
  fetch('/api/prefs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ speed: v })
  });
}

function setSleep(mins) {
  if (sleepTimer) clearTimeout(sleepTimer);
  if (mins == 0) return;
  sleepTimer = setTimeout(() => {
    audio.pause();
    alert('Sleep timer finished');
  }, mins * 60 * 1000);
}

function skip(sec) {
  audio.currentTime = Math.max(0, Math.min(audio.duration || 0, audio.currentTime + sec));
}

let pendingSeek = null;
function seekChapter(start) {
  pendingSeek = start;
  fileIndex = 0;
  if (!audio.src) loadFile(true);
  else {
    try { audio.currentTime = start; } catch (e) {}
    pendingSeek = null;
    const play = audio.play();
    if (play) play.catch(() => {});
    saveNow();
  }
  updateActive();
}
function renderTracks() {
  const ul = document.getElementById('tracks');
  const chapters = (book.kind === 'music') ? [] : (book.chapters || []);
  if (chapters.length) {
    ul.innerHTML = chapters.map(ch => {
      const label = esc(ch.title || '');
      const start = fmtTime(ch.start);
      const hasLen = ch.length != null && ch.length !== '';
      const time = hasLen
        ? '<span class="ch-len">' + fmtTime(ch.length) + '</span><span class="ch-start">starts ' + start + '</span>'
        : '<span class="ch-len">starts ' + start + '</span>';
      return '<li data-start="' + ch.start + '" onclick="seekChapter(' + ch.start + ')"><span class="ch-title">' + label + '</span><span class="ch-time">' + time + '</span></li>';
    }).join('');
    updateActive();
    return;
  }
  ul.innerHTML = book.files.map((f, i) => {
    const safeName = esc(f.name || '');
    const dur = Number(f.duration) > 0 ? '<span class="ch-len">' + fmtTime(f.duration) + '</span>' : '';
    return `<li data-idx="${i}">
      <span class="ch-title">${safeName}</span>
      <span class="ch-time">${dur}</span>
      <button type="button" onclick="playIndex(${i})">Play</button>
    </li>`;
  }).join('');
  updateActive();
}

function updateActive() {
  const chapters = (book && book.kind !== 'music' && book.chapters) || [];
  if (chapters.length) {
    const now = audio.currentTime || 0;
    let active = 0;
    chapters.forEach((ch, i) => { if (now + 0.5 >= ch.start) active = i; });
    document.querySelectorAll('#tracks li').forEach((li, i) => li.classList.toggle('active', i === active));
    document.getElementById('track-label').textContent = chapters[active] ? chapters[active].title : '';
    document.getElementById('download-link').href = '/stream/' + BOOK_ID + '/0?download=1';
    return;
  }
  document.querySelectorAll('#tracks li').forEach((li, i) => {
    li.classList.toggle('active', i === fileIndex);
  });
  document.getElementById('track-label').textContent = `Track ${fileIndex + 1} / ${book.files.length}`;
  document.getElementById('download-link').href = `/stream/${BOOK_ID}/${fileIndex}?download=1`;
}

function playIndex(i) {
  fileIndex = i;
  preloadedIndex = -1;
  updateActive();
  loadFile(true);
}

function loadFile(andPlay) {
  const saved = progress[BOOK_ID] || {};
  const time = (saved.fileIndex === fileIndex) ? (saved.time || 0) : 0;
  const url = `/stream/${encodeURIComponent(BOOK_ID)}/${fileIndex}`;
  document.getElementById('progress-info').textContent = 'Loading...';
  audio.src = url;
  audio.onerror = () => {
    const err = audio.error;
    document.getElementById('progress-info').textContent = 'Stream error: ' + (err ? err.code : 'unknown');
  };
  audio.onloadedmetadata = () => {
    const jump = pendingSeek != null ? pendingSeek : time;
    pendingSeek = null;
    if (jump > 1 && Math.abs(audio.currentTime - jump) > 2) {
      try { audio.currentTime = jump; } catch (e) {}
    }
    updateActive();
  };
  if (andPlay) {
    const p = audio.play();
    if (p) p.then(() => {
      document.getElementById('play-btn').textContent = 'Pause';
      document.getElementById('progress-info').textContent = 'Playing';
    }).catch(e => {
      document.getElementById('play-btn').textContent = 'Play';
      document.getElementById('progress-info').textContent = 'Could not play: ' + e.message;
    });
  }
}

function togglePlay() {
  const btn = document.getElementById('play-btn');
  if (!book || !book.files || !book.files.length) {
    document.getElementById('progress-info').textContent = 'Book still loading';
    return;
  }
  if (!audio.src || audio.src === location.href) {
    playIndex(fileIndex || 0);
    return;
  }
  if (audio.paused) {
    btn.textContent = 'Loading...';
    const p = audio.play();
    if (p) {
      p.then(() => {
        btn.textContent = 'Pause';
        document.getElementById('progress-info').textContent = 'Playing';
      }).catch(e => {
        btn.textContent = 'Play';
        document.getElementById('progress-info').textContent = 'Could not play: ' + e.message;
      });
    }
  } else {
    audio.pause();
    btn.textContent = 'Play';
    document.getElementById('progress-info').textContent = 'Paused';
  }
}

function preloadNext() {
  if (!book || fileIndex >= book.files.length - 1) {
    preloadedIndex = -1;
    return;
  }
  const nextIdx = fileIndex + 1;
  if (preloadedIndex === nextIdx) return;
  nextAudio.src = `/stream/${BOOK_ID}/${nextIdx}`;
  nextAudio.preload = 'metadata';
  nextAudio.load();
  preloadedIndex = nextIdx;
}

function next() {
  if (fileIndex >= book.files.length - 1) return;
  const wasPlaying = !audio.paused;
  fileIndex++;
  updateActive();
  if (preloadedIndex === fileIndex && nextAudio.readyState >= 2) {
    audio.src = nextAudio.src;
    audio.currentTime = 0;
    if (wasPlaying) audio.play().catch(() => {});
  } else {
    loadFile(wasPlaying);
  }
  saveNow();
}

function prev() {
  if (fileIndex <= 0) return;
  const wasPlaying = !audio.paused;
  fileIndex--;
  updateActive();
  preloadedIndex = -1;
  loadFile(wasPlaying);
  saveNow();
}

audio.onended = () => { if (fileIndex < book.files.length - 1) next(); };

let saveTimer, lastSave = 0, listened = 0, lastTick = 0;
function saveNow() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fetch('/api/progress', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: BOOK_ID, fileIndex, time: audio.currentTime })
    }).catch(() => {});
    progress[BOOK_ID] = { fileIndex, time: audio.currentTime };
  }, 500);
}
async function autoBookmark() {
  try {
    await fetch('/api/bookmarks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: BOOK_ID, action: 'add', fileIndex, time: audio.currentTime, note: 'auto', auto: true })
    });
    loadBookmarks();
  } catch (e) {}
}
audio.onpause = saveNow;
audio.ontimeupdate = () => {
  const now = Date.now();
  if (!audio.paused && lastTick) {
    const delta = (now - lastTick) / 1000;
    if (delta > 0 && delta < 2) listened += delta;
  }
  lastTick = now;
  if (!audio.paused && listened >= 300) {
    listened = 0;
    autoBookmark();
  }
  if (audio.duration && (audio.duration - audio.currentTime) < 20) preloadNext();
  if (audio.currentTime - lastSave > 10) { saveNow(); lastSave = audio.currentTime; }
  if (book && book.chapters && book.chapters.length) updateActive();
  // Time info
  if (audio.duration) {
    const rem = audio.duration - audio.currentTime;
    document.getElementById('time-info').textContent =
      fmtTime(audio.currentTime) + ' / ' + fmtTime(audio.duration) + ' (' + fmtTime(rem) + ' left)';
  }
};

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowRight') skip(15);
  if (e.code === 'ArrowLeft') skip(-15);
  if (e.code === 'ArrowUp') setSpeed(Math.min(2, parseFloat(document.getElementById('speed').value) + 0.25));
  if (e.code === 'ArrowDown') setSpeed(Math.max(0.75, parseFloat(document.getElementById('speed').value) - 0.25));
});

load();

async function saveMeta() {
  const title = document.getElementById('meta-title').value.trim();
  const author = document.getElementById('meta-author').value.trim();
  const cover_url = document.getElementById('meta-cover').value.trim();
  const msg = document.getElementById('meta-msg');
  msg.textContent = 'Saving...';
  try {
    const r = await fetch('/api/set-meta', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ id: BOOK_ID, title, author, cover_url })
    });
    if (r.ok) {
      msg.textContent = 'Saved. Refreshing...';
      setTimeout(() => location.reload(), 800);
    } else {
      msg.textContent = 'Failed to save';
    }
  } catch (e) {
    msg.textContent = 'Error: ' + e.message;
  }
}

async function refreshFinishBtn() {
  const btn = document.getElementById('finish-btn');
  if (!btn) return;
  try {
    const r = await fetch('/api/finished');
    const ids = await r.json();
    const done = ids.indexOf(BOOK_ID) !== -1;
    btn.dataset.done = done ? '1' : '0';
    btn.textContent = done ? 'Return to Library' : 'Mark Finished';
  } catch (e) {}
}

async function toggleFinished() {
  const btn = document.getElementById('finish-btn');
  const done = btn.dataset.done === '1';
  await fetch('/api/finished', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: BOOK_ID, action: done ? 'remove' : 'add' })
  });
  refreshFinishBtn();
}

async function addToPlaylist(mode) {
  const r = await fetch('/api/playlists');
  const lists = (await r.json()).filter(p => p.mine);
  const names = lists.map(p => p.name);
  const hint = names.length ? '\\nYours: ' + names.join(', ') : '';
  const choice = prompt('Playlist name' + hint, names[0] || '');
  if (!choice) return;
  const name = choice.trim();
  if (!name) return;
  let pl = lists.find(p => p.name === name);
  if (!pl) {
    const created = await fetch('/api/playlists', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'create', name})
    });
    if (!created.ok) { alert(await created.text()); return; }
    pl = await created.json();
  }
  const body = {action: 'add', id: pl.id, bookId: BOOK_ID};
  if (mode === 'track') body.fileIndex = fileIndex;
  const added = await fetch('/api/playlists', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!added.ok) { alert(await added.text()); return; }
  alert((mode === 'track' ? 'Track' : 'All tracks') + ' added to "' + name + '"');
}

async function addToCollection() {
  const r = await fetch('/api/collections');
  const cols = await r.json();
  const names = Object.keys(cols);
  if (!names.length) {
    const name = prompt('No collections yet. Enter a name to create one:');
    if (!name) return;
    await fetch('/api/collections', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'create', name}) });
    await fetch('/api/collections', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'add', name, book_id: BOOK_ID}) });
    alert('Added to "' + name + '"');
    return;
  }
  const choice = prompt('Add to collection:\\n' + names.map((n,i) => (i+1) + '. ' + n).join('\\n') + '\\n\\nEnter number or new name:');
  if (!choice) return;
  let name = choice;
  const num = parseInt(choice);
  if (!isNaN(num) && num >= 1 && num <= names.length) name = names[num-1];
  else if (!names.includes(choice)) {
    await fetch('/api/collections', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'create', name: choice}) });
    name = choice;
  }
  await fetch('/api/collections', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({action:'add', name, book_id: BOOK_ID}) });
  alert('Added to "' + name + '"');
}
</script>
</body>
</html>'''

PASSWORD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Change Password · Audiobooks</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #111; color: #eee; }
  .card { background: #1a1a1a; padding: 2rem; border-radius: 12px; width: 100%; max-width: 360px; }
  h1 { margin: 0 0 1.5rem; text-align: center; }
  input { width: 100%; padding: 0.75rem; margin: 0.5rem 0; border: none; border-radius: 6px; background: #222; color: #eee; box-sizing: border-box; }
  button { width: 100%; padding: 0.75rem; margin-top: 1rem; background: #4a9eff; color: white; border: none; border-radius: 6px; font-size: 1rem; cursor: pointer; }
  .msg { margin-top: 1rem; text-align: center; }
  a { color: #4a9eff; display: block; text-align: center; margin-top: 1rem; }
</style>
</head>
<body>
<div class="card">
  <h1>Change Password</h1>
  <input id="current" type="password" placeholder="Current password" required>
  <input id="new" type="password" placeholder="New password" required>
  <input id="confirm" type="password" placeholder="Confirm new password" required>
  <button onclick="change()">Update Password</button>
  <div class="msg" id="msg"></div>
  <a href="/">Back to Library</a>
</div>
<script>
async function change() {
  const current = document.getElementById('current').value;
  const neu = document.getElementById('new').value;
  const confirm = document.getElementById('confirm').value;
  const msg = document.getElementById('msg');
  if (neu !== confirm) { msg.textContent = 'New passwords do not match'; msg.style.color = '#f66'; return; }
  if (neu.length < 4) { msg.textContent = 'Password too short'; msg.style.color = '#f66'; return; }
  const r = await fetch('/api/password', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({current, new: neu})
  });
  if (r.ok) {
    msg.textContent = 'Password updated successfully';
    msg.style.color = '#4c4';
    document.getElementById('current').value = '';
    document.getElementById('new').value = '';
    document.getElementById('confirm').value = '';
  } else {
    const text = await r.text();
    msg.textContent = text || 'Failed';
    msg.style.color = '#f66';
  }
}
</script>
</body>
</html>'''

USERS_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Users · Audiobooks</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; background: #111; color: #eee; }
  header { padding: 1rem; background: #1a1a1a; display: flex; justify-content: space-between; align-items: center; }
  .container { padding: 1rem; max-width: 600px; margin: 0 auto; }
  input, button { padding: 0.6rem; margin: 0.3rem 0; border: none; border-radius: 6px; }
  input { background: #222; color: #eee; width: 200px; }
  button { background: #4a9eff; color: white; cursor: pointer; }
  .danger { background: #c44; }
  table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
  td, th { padding: 0.5rem; text-align: left; border-bottom: 1px solid #333; }
  a { color: #4a9eff; }
</style>
</head>
<body>
<header>
  <h1>Manage Users</h1>
  <a href="/"><button>Back to Library</button></a>
</header>
<div class="container">
  <h3>Add User</h3>
  <input id="newuser" placeholder="Username">
  <input id="newpass" type="password" placeholder="Password">
  <button onclick="addUser()">Add</button>

  <h3>Existing Users</h3>
  <div id="userlist">Loading...</div>
</div>
<script>
async function loadUsers() {
  const r = await fetch('/api/users');
  const users = await r.json();
  document.getElementById('userlist').innerHTML = `
    <table>
      <tr><th>Username</th><th>Actions</th></tr>
      ${users.map(u => `
        <tr>
          <td>${u.username}</td>
          <td>
            <input id="pass-${u.username}" type="password" placeholder="New password" style="width:140px">
            <button onclick="changePass('${u.username}')">Change</button>
            ${u.username !== 'admin' ? `<button class="danger" onclick="delUser('${u.username}')">Delete</button>` : ''}
          </td>
        </tr>`).join('')}
    </table>`;
}

async function addUser() {
  const username = document.getElementById('newuser').value.trim();
  const password = document.getElementById('newpass').value;
  if (!username || !password) return alert('Fill both fields');
  await fetch('/api/users', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'add', username, password})
  });
  document.getElementById('newuser').value = '';
  document.getElementById('newpass').value = '';
  loadUsers();
}

async function changePass(username) {
  const password = document.getElementById('pass-' + username).value;
  if (!password) return alert('Enter a new password');
  await fetch('/api/users', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'change', username, password})
  });
  document.getElementById('pass-' + username).value = '';
  alert('Password updated');
}

async function delUser(username) {
  if (!confirm('Delete ' + username + '?')) return;
  await fetch('/api/users', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'delete', username})
  });
  loadUsers();
}

loadUsers();
</script>
</body>
</html>'''

def main():
    log.info(f"Audiobook server on port {PORT}")
    log.info(f"Library: {AUDIO_DIR}")
    log.info(f"Music library: {MUSIC_DIR}")
    log.info(f"Cover cache: {COVER_CACHE}")
    log.info(f"Users file: {USERS_FILE}")
    log.info(f"Library exists: {AUDIO_DIR.exists()}")
    log.info(f"Music library exists: {MUSIC_DIR.is_dir()}")
    log.info(f"Log file: {LOG_FILE} (level {logging.getLevelName(log.level)})")

    use_https = os.environ.get("HTTPS", "").lower() in ("1", "true", "yes")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    if use_https:
        cert = ROOT / "server.crt"
        key = ROOT / "server.key"
        if not cert.exists() or not key.exists():
            import subprocess
            log.info("Generating self-signed certificate...")
            subprocess.check_call([
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "3650", "-nodes",
                "-subj", "/CN=audiobook-server"
            ])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        log.info(f"HTTPS enabled (self-signed). Open https://YOUR_HOST:{PORT}/")
        log.info("Browser will warn about the certificate. For a real certificate,")
        log.info("leave HTTPS off and use Whatbox Managed Links instead.")
    else:
        log.info(f"HTTP on port {PORT}")
        if HTTPS_REDIRECT:
            log.info(f"Plain HTTP redirects to {HTTPS_REDIRECT}")
            log.info(f"Subsonic API: {HTTPS_REDIRECT}/rest")
        else:
            log.info("No HTTPS redirect yet. Create https-url.txt next to server.py")
            log.info("with one line, your Managed Link, for example:")
            log.info("  https://audiobooks.YOUR_SLOT.box.ca")
        log.info("For HTTPS: on Whatbox Manage Links, add a custom app named")
        log.info(f"  audiobooks  pointing at port {PORT}")
        log.info("Then open https://audiobooks.YOUR_SLOT.box.ca/")

    httpd.serve_forever()
