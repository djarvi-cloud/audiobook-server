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
from audiobook.config import MUSIC_FOLDER_ID, iso_now, log
from audiobook.store import playlists, playqueues, progress, save_playlists, save_playqueue, save_progress
from audiobook.auth import q1, q_bool, subsonic_user
from audiobook.tags import guess_duration
from audiobook.library import artists_from, book_author, book_created, cover_file, folder_books, get_book_by_id, get_books_cached, get_music_cached, include_books, include_music, library_roots, music_album_by_id, music_album_obj, music_albums, music_artist_key, music_artist_name, music_artists_from, music_cover_file, music_dir_ready, music_folder_name, music_song_by_id, music_song_obj, resolve_music, selected_books, sort_key_name
from audiobook.stream import cover_mime_for, send_book_zip, send_image_bytes, send_music_album_zip, send_song_file

SUBSONIC_VERSION = "1.16.1"

def xml_escape(value):
    text = str(value)
    amp = "&" + "amp;"
    lt = "&" + "lt;"
    gt = "&" + "gt;"
    quot = "&" + "quot;"
    return text.replace("&", amp).replace("<", lt).replace(">", gt).replace('"', quot)

def xml_node(name, value):
    if isinstance(value, list):
        return "".join(xml_node(name, item) for item in value)
    if isinstance(value, dict):
        attrs = []
        children = []
        list_names = {
            "album": "album", "song": "song", "artist": "artist", "index": "index",
            "child": "child", "musicFolder": "musicFolder", "bookmark": "bookmark",
            "playlist": "playlist", "entry": "entry", "user": "user", "genre": "genre",
            "folder": "folder", "version": "version",
        }
        for key, item in value.items():
            if item is None:
                continue
            if isinstance(item, list):
                child_name = list_names.get(key, key[:-1] if key.endswith("s") else key)
                for one in item:
                    if isinstance(one, (dict, list)):
                        children.append(xml_node(child_name, one))
                    else:
                        children.append("<" + child_name + ">" + xml_escape(one) + "</" + child_name + ">")
            elif isinstance(item, dict):
                children.append(xml_node(key, item))
            else:
                shown = "true" if item is True else "false" if item is False else item
                attrs.append(' ' + key + '="' + xml_escape(shown) + '"')
        inner = "".join(children)
        if inner:
            return "<" + name + "".join(attrs) + ">" + inner + "</" + name + ">"
        return "<" + name + "".join(attrs) + "/>"
    shown = "true" if value is True else "false" if value is False else value
    return "<" + name + ">" + xml_escape(shown) + "</" + name + ">"

def send_subsonic(handler, body, fmt, failed=False):
    status = "failed" if failed else "ok"
    if fmt == "json":
        payload = {
            "subsonic-response": {
                "status": status,
                "version": SUBSONIC_VERSION,
                "type": "audiobook-server",
                "serverVersion": "1.0.0",
                "openSubsonic": True,
            }
        }
        payload["subsonic-response"].update(body or {})
        data = json.dumps(payload).encode()
        ctype = "application/json"
    else:
        inner = "".join(xml_node(key, value) for key, value in (body or {}).items())
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<subsonic-response xmlns="http://subsonic.org/restapi" status="{status}" '
            f'version="{SUBSONIC_VERSION}" type="audiobook-server" serverVersion="1.0.0" '
            f'openSubsonic="true">{inner}</subsonic-response>'
        )
        data = xml.encode()
        ctype = "text/xml"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)

def subsonic_error(handler, code, message, fmt):
    log.warning(f"Subsonic error {code}: {message}")
    send_subsonic(handler, {"error": {"code": int(code), "message": message}}, fmt, failed=True)

def subsonic_params(handler, parsed):
    qs = {k: list(v) for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    if handler.command == "POST":
        length = int(handler.headers.get("Content-Length", 0) or 0)
        raw = handler.rfile.read(length) if length else b""
        ctype = (handler.headers.get("Content-Type") or "").lower()
        if raw and "json" in ctype:
            try:
                data = json.loads(raw.decode())
                if isinstance(data, dict):
                    for key, value in data.items():
                        if isinstance(value, list):
                            qs.setdefault(key, [str(v) for v in value])
                        elif value is not None:
                            qs.setdefault(key, [str(value)])
            except Exception:
                pass
        elif raw:
            for key, value in parse_qs(raw.decode(errors="replace"), keep_blank_values=True).items():
                qs.setdefault(key, value)
    return qs

def artist_key(name):
    name = name or "Unknown"
    return "ar:" + base64.urlsafe_b64encode(name.encode()).decode().rstrip("=")

def artist_name(aid):
    if not aid.startswith("ar:"):
        return None
    raw = aid[3:]
    raw += "=" * ((4 - len(raw) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(raw).decode()
    except Exception:
        return None

def album_key(book):
    return "al:" + book["id"]

def song_key(book, index):
    return f"tr:{book['id']}:{index}"

def parse_song_key(sid):
    if not sid.startswith("tr:"):
        return None, None
    book_id, sep, idx = sid[3:].rpartition(":")
    if not sep:
        return None, None
    try:
        return book_id, int(idx)
    except ValueError:
        return None, None

def song_obj(book, index, info):
    name = info.get("name") or f"Part {index + 1}"
    ext = Path(name).suffix.lower().lstrip(".")
    if ext in ("m4b", "m4a", "mp4"):
        mime = "audio/mp4"
    elif ext == "mp3":
        mime = "audio/mpeg"
    else:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    size = int(info.get("size") or 0)
    author = book_author(book)
    return {
        "id": song_key(book, index),
        "parent": album_key(book),
        "isDir": False,
        "title": Path(name).stem or name,
        "album": book.get("name") or "Untitled",
        "artist": author,
        "track": index + 1,
        "coverArt": album_key(book),
        "size": size,
        "contentType": mime,
        "suffix": ext or "mp3",
        "duration": guess_duration(size),
        "bitRate": 128,
        "path": info.get("rel") or name,
        "albumId": album_key(book),
        "artistId": artist_key(author),
        "type": "music",
        "genre": "Audiobook",
    }

def album_obj(book, with_songs=False):
    author = book_author(book)
    songs = [song_obj(book, i, f) for i, f in enumerate(book.get("files") or [])]
    obj = {
        "id": album_key(book),
        "name": book.get("name") or "Untitled",
        "title": book.get("name") or "Untitled",
        "album": book.get("name") or "Untitled",
        "artist": author,
        "artistId": artist_key(author),
        "coverArt": album_key(book),
        "songCount": len(songs),
        "duration": sum(s["duration"] for s in songs),
        "created": book_created(book),
        "genre": "Audiobook",
    }
    if book.get("series"):
        obj["series"] = book.get("series")
    if book.get("seriesIndex") not in (None, ""):
        obj["seriesIndex"] = book.get("seriesIndex")
    if with_songs:
        obj["song"] = songs
    return obj

def find_book(sid):
    if sid.startswith("al:"):
        return get_book_by_id(sid[3:]), None
    if sid.startswith("tr:"):
        book_id, index = parse_song_key(sid)
        book = get_book_by_id(book_id) if book_id else None
        return book, index
    book = get_book_by_id(sid)
    if book:
        return book, None
    return None, None

def set_listen_progress(username, book, file_index, seconds):
    if username not in progress:
        progress[username] = {}
    progress[username][book["id"]] = {
        "fileIndex": int(file_index),
        "time": float(seconds),
        "updated": int(time.time() * 1000),
    }
    save_progress()

def progress_bookmark(username, book):
    saved = (progress.get(username) or {}).get(book["id"])
    if not saved:
        return None
    files = book.get("files") or []
    index = int(saved.get("fileIndex") or 0)
    if index < 0 or index >= len(files):
        index = 0
    if not files:
        return None
    seconds = float(saved.get("time") or 0)
    return {
        "position": int(seconds * 1000),
        "username": username,
        "comment": "progress",
        "created": iso_now((saved.get("updated") or 0) / 1000),
        "changed": iso_now((saved.get("updated") or 0) / 1000),
        "entry": song_obj(book, index, files[index]),
    }

def page_slice(items, qs):
    try:
        offset = max(0, int(q1(qs, "offset", "0") or 0))
    except ValueError:
        offset = 0
    try:
        size = int(q1(qs, "size", "50") or 50)
    except ValueError:
        size = 50
    if size <= 0:
        size = 50
    return items[offset:offset + size]

def artist_index_entries(folder_id=""):
    entries = []
    if include_books(folder_id):
        grouped = artists_from(selected_books(folder_id))
        for name, items in grouped.items():
            entries.append({"id": artist_key(name), "name": name, "albumCount": len(items)})
    if include_music(folder_id):
        grouped = music_artists_from(music_albums(folder_id))
        for name, items in grouped.items():
            entries.append({"id": music_artist_key(name), "name": name, "albumCount": len(items)})
    return entries

def grouped_artist_indexes(folder_id=""):
    letters = {}
    for artist in artist_index_entries(folder_id):
        label = sort_key_name(artist["name"]).strip()[:1].upper() or "#"
        if not label.isalpha():
            label = "#"
        letters.setdefault(label, []).append(artist)
    indexes = []
    for label in sorted(letters):
        artists = sorted(letters[label], key=lambda a: sort_key_name(a["name"]).lower())
        indexes.append({"name": label, "artist": artists})
    return indexes

def listed_album_objs(folder_id=""):
    items = []
    if include_books(folder_id):
        for book in selected_books(folder_id):
            items.append(("book", book, album_obj(book)))
    if include_music(folder_id):
        for album in music_albums(folder_id):
            items.append(("music", album, music_album_obj(album)))
    return items

def listed_songs(folder_id=""):
    songs = []
    if include_books(folder_id):
        for book in selected_books(folder_id):
            for i, info in enumerate(book.get("files") or []):
                songs.append(song_obj(book, i, info))
    if include_music(folder_id):
        for album in music_albums(folder_id):
            for info in album.get("files") or []:
                songs.append(music_song_obj(album, info))
    return songs

def resolve_song(sid):
    if not sid:
        return None
    if sid.startswith("ms:"):
        album, info = music_song_by_id(sid)
        if album and info:
            return music_song_obj(album, info)
        return None
    book, index = find_book(sid)
    if book and index is not None and 0 <= index < len(book.get("files") or []):
        return song_obj(book, index, book["files"][index])
    return None

def send_cover_art(handler, sid):
    if sid.startswith("pl:"):
        pl = playlists.get(sid[3:]) or playlists.get(sid)
        if not pl:
            handler.send_error(404)
            return
        for song_id in pl.get("songIds") or []:
            if send_cover_art_media(handler, song_id, missing_ok=True):
                return
        handler.send_error(404)
        return
    if not send_cover_art_media(handler, sid, missing_ok=True):
        handler.send_error(404)

def send_cover_art_media(handler, sid, missing_ok=False):
    if sid.startswith("mar:"):
        name = music_artist_name(sid)
        albums = [a for a in get_music_cached() if (a.get("artist") or "Unknown") == name] if name else []
        for album in albums:
            path = music_cover_file(album)
            if path and path.is_file():
                send_image_bytes(handler, path.read_bytes(), cover_mime_for(path))
                return True
        return False
    if sid.startswith("mal:") or sid.startswith("ms:"):
        if sid.startswith("mal:"):
            album, info = music_album_by_id(sid), None
        else:
            album, info = music_song_by_id(sid)
        if not album:
            return False
        path = music_cover_file(album, info)
        if path and path.is_file():
            send_image_bytes(handler, path.read_bytes(), cover_mime_for(path))
            return True
        return False
    book, _idx = find_book(sid)
    if book:
        path = cover_file(book)
        if path:
            send_image_bytes(handler, path.read_bytes(), cover_mime_for(path) if hasattr(path, "suffix") else "image/jpeg")
            return True
    if sid.startswith("ar:"):
        name = artist_name(sid)
        books = [b for b in get_books_cached() or [] if book_author(b) == name] if name else []
        for book in books:
            path = cover_file(book)
            if path:
                send_image_bytes(handler, path.read_bytes(), cover_mime_for(path))
                return True
    return False if missing_ok else False

def next_playlist_id():
    n = 0
    for key in playlists:
        try:
            n = max(n, int(key))
        except ValueError:
            pass
    return str(n + 1)

def playlist_visible(pl, username):
    return (pl.get("owner") == username) or bool(pl.get("public"))

def playlist_owned(pl, username):
    return pl.get("owner") == username

def playlist_get(pid):
    if pid is None or pid == "":
        return None
    return playlists.get(str(pid))

def playlist_entries(pl):
    entries = []
    for sid in pl.get("songIds") or []:
        song = resolve_song(sid)
        if song:
            entries.append(song)
    return entries

def playlist_obj(pl, with_entries=False):
    entries = playlist_entries(pl)
    obj = {
        "id": str(pl.get("id")),
        "name": pl.get("name") or "Playlist",
        "comment": pl.get("comment") or "",
        "owner": pl.get("owner") or "",
        "public": bool(pl.get("public")),
        "songCount": len(entries),
        "duration": sum(int(s.get("duration") or 0) for s in entries),
        "created": pl.get("created") or iso_now(),
        "changed": pl.get("changed") or iso_now(),
        "coverArt": "pl:" + str(pl.get("id")),
    }
    if with_entries:
        obj["entry"] = entries
    return obj

def new_playlist(username, name, song_ids):
    pid = next_playlist_id()
    now = iso_now()
    pl = {
        "id": pid,
        "name": name or "Playlist",
        "comment": "",
        "owner": username,
        "public": False,
        "created": now,
        "changed": now,
        "songIds": list(song_ids or []),
    }
    playlists[pid] = pl
    save_playlists()
    return pl

def resolved_song_positions(pl):
    positions = []
    for n, sid in enumerate(pl.get("songIds") or []):
        if resolve_song(sid):
            positions.append(n)
    return positions

def drop_resolved_indexes(pl, indexes):
    positions = resolved_song_positions(pl)
    drop = set()
    for raw in indexes:
        if not str(raw).lstrip("-").isdigit():
            continue
        i = int(raw)
        if 0 <= i < len(positions):
            drop.add(positions[i])
    if drop:
        pl["songIds"] = [sid for n, sid in enumerate(pl.get("songIds") or []) if n not in drop]

def song_ids_for_item(item_id):
    book = get_book_by_id(item_id)
    if not book:
        return []
    if str(item_id).startswith("mal:") or book.get("kind") == "music":
        album = music_album_by_id(item_id)
        if not album:
            return []
        return [info["id"] for info in album.get("files") or [] if info.get("id")]
    return [song_key(book, i) for i in range(len(book.get("files") or []))]

def web_playlist_tracks(pl):
    tracks = []
    for sid in pl.get("songIds") or []:
        song = resolve_song(sid)
        if not song:
            continue
        album_id = song.get("albumId") or ""
        if album_id.startswith("al:"):
            book_id = album_id[3:]
            file_index = max(0, int(song.get("track") or 1) - 1)
        elif album_id.startswith("mal:"):
            book_id = album_id
            album = music_album_by_id(album_id)
            files = (album or {}).get("files") or []
            file_index = next((i for i, info in enumerate(files) if info.get("id") == song.get("id")), 0)
        else:
            continue
        tracks.append({
            "songId": song.get("id"),
            "title": song.get("title") or "",
            "artist": song.get("artist") or "",
            "album": song.get("album") or "",
            "bookId": book_id,
            "fileIndex": file_index,
            "duration": int(song.get("duration") or 0),
        })
    return tracks

def web_playlist(pl, username, with_songs=False):
    tracks = web_playlist_tracks(pl)
    obj = {
        "id": str(pl.get("id")),
        "name": pl.get("name") or "Playlist",
        "owner": pl.get("owner") or "",
        "public": bool(pl.get("public")),
        "mine": pl.get("owner") == username,
        "songCount": len(tracks),
        "changed": pl.get("changed") or "",
    }
    if with_songs:
        obj["songs"] = tracks
    return obj

def visible_playlists(username):
    rows = [web_playlist(pl, username) for pl in playlists.values() if playlist_visible(pl, username)]
    rows.sort(key=lambda item: (0 if item["mine"] else 1, (item.get("name") or "").lower()))
    return rows

def playlist_page_id(pid):
    return isinstance(pid, str) and pid.isdigit() and 0 < len(pid) < 12

def mutate_playlist(username, data):
    action = (data or {}).get("action")
    if action == "create":
        name = (data.get("name") or "").strip()
        if not name:
            return 400, "Name required"
        pl = new_playlist(username, name, [])
        return 200, web_playlist(pl, username, with_songs=True)
    pid = str(data.get("id") or "")
    pl = playlist_get(pid) if playlist_page_id(pid) else None
    if not pl or not playlist_visible(pl, username):
        return 404, "Playlist not found"
    if not playlist_owned(pl, username):
        return 403, "Not your playlist"
    if action == "rename":
        name = (data.get("name") or "").strip()
        if not name:
            return 400, "Name required"
        pl["name"] = name
    elif action == "public":
        pl["public"] = bool(data.get("public"))
    elif action == "delete":
        playlists.pop(str(pl["id"]), None)
        save_playlists()
        return 200, {"ok": True}
    elif action == "add":
        added = []
        song_id = data.get("songId") or ""
        book_id = data.get("bookId") or ""
        file_index = data.get("fileIndex", None)
        if song_id:
            song = resolve_song(song_id)
            if song and song.get("id"):
                added.append(song["id"])
        elif book_id and file_index is not None and str(file_index).lstrip("-").isdigit():
            ids = song_ids_for_item(book_id)
            i = int(file_index)
            if 0 <= i < len(ids):
                added.append(ids[i])
        elif book_id:
            added.extend(song_ids_for_item(book_id))
        if not added:
            return 400, "Nothing to add"
        pl.setdefault("songIds", []).extend(added)
    elif action == "remove":
        drop_resolved_indexes(pl, [data.get("index")])
    elif action == "move":
        try:
            index = int(data.get("index"))
            direction = int(data.get("direction"))
        except (TypeError, ValueError):
            return 400, "Bad move"
        positions = resolved_song_positions(pl)
        j = index + direction
        if direction not in (-1, 1) or not (0 <= index < len(positions)):
            return 400, "Bad move"
        if 0 <= j < len(positions):
            ids = pl.setdefault("songIds", [])
            a, b = positions[index], positions[j]
            ids[a], ids[b] = ids[b], ids[a]
    else:
        return 400, "Unknown action"
    pl["changed"] = iso_now()
    save_playlists()
    return 200, web_playlist(pl, username, with_songs=True)

def handle_subsonic(handler, parsed):
    method = parsed.path[len("/rest/"):]
    if method.endswith(".view"):
        method = method[:-5]
    method = method.strip("/").split("/")[-1]
    qs = subsonic_params(handler, parsed)
    fmt = q1(qs, "f", "xml").lower()
    if fmt not in ("json", "xml", "jsonp"):
        fmt = "xml"
    username, err = subsonic_user(qs)
    if method != "ping" and err:
        subsonic_error(handler, 40, err, "json" if fmt == "json" else "xml")
        return
    if method == "ping" and err and q1(qs, "u"):
        subsonic_error(handler, 40, err, "json" if fmt == "json" else "xml")
        return
    out_fmt = "json" if fmt == "json" else "xml"
    if method in ("ping", "getCoverArt", "stream", "download", "scrobble"):
        log.debug(f"Subsonic {method} user={username or '-'}")
    else:
        log.info(f"Subsonic {method} user={username or '-'}")

    def ok(body=None):
        send_subsonic(handler, body or {}, out_fmt)

    if method == "ping":
        ok()
        return
    if method == "getLicense":
        ok({"license": {"valid": True, "email": username or "", "licenseExpires": "2099-01-01T00:00:00Z"}})
        return
    if method == "getOpenSubsonicExtensions":
        ok({"openSubsonicExtensions": [
            {"name": "formPost", "versions": [1]},
        ]})
        return
    if method == "tokenInfo":
        ok({"tokenInfo": {"username": username}})
        return
    if method in ("getMusicFolders",):
        folders = []
        for i, root in enumerate(library_roots()):
            folders.append({"id": f"mf:{i}", "name": root.name or str(root)})
        if music_dir_ready():
            folders.append({"id": MUSIC_FOLDER_ID, "name": music_folder_name()})
        ok({"musicFolders": {"musicFolder": folders}})
        return
    if method == "getIndexes":
        ok({"indexes": {"lastModified": int(time.time() * 1000), "index": grouped_artist_indexes(q1(qs, "musicFolderId"))}})
        return
    if method == "getMusicDirectory":
        mid = q1(qs, "id")
        if mid == MUSIC_FOLDER_ID or mid == "mf:music":
            children = []
            for name, items in sorted(music_artists_from().items(), key=lambda pair: sort_key_name(pair[0]).lower()):
                children.append({"id": music_artist_key(name), "isDir": True, "title": name, "artist": name, "albumCount": len(items)})
            ok({"directory": {"id": MUSIC_FOLDER_ID, "name": music_folder_name(), "child": children}})
            return
        if mid.startswith("mf:"):
            books = folder_books(mid)
            children = []
            for name in sorted(artists_from(books), key=lambda n: sort_key_name(n).lower()):
                children.append({"id": artist_key(name), "isDir": True, "title": name, "artist": name})
            ok({"directory": {"id": mid, "name": "Library", "child": children}})
            return
        if mid.startswith("mar:"):
            name = music_artist_name(mid) or "Unknown"
            albums = [a for a in get_music_cached() if (a.get("artist") or "Unknown") == name]
            children = []
            for album in sorted(albums, key=lambda a: (a.get("name") or "").lower()):
                obj = music_album_obj(album)
                children.append({"id": obj["id"], "parent": mid, "isDir": True, "title": obj["name"], "artist": name, "coverArt": obj["coverArt"]})
            ok({"directory": {"id": mid, "name": name, "parent": MUSIC_FOLDER_ID, "child": children}})
            return
        if mid.startswith("mal:"):
            album = music_album_by_id(mid)
            if not album:
                subsonic_error(handler, 70, "Not found", out_fmt)
                return
            songs = [music_song_obj(album, f) for f in album.get("files") or []]
            ok({"directory": {"id": album["id"], "parent": album.get("artistId"), "name": album.get("name") or "Untitled", "child": songs}})
            return
        if mid.startswith("ar:"):
            name = artist_name(mid) or "Unknown"
            books = [b for b in folder_books("") if book_author(b) == name]
            children = []
            for book in sorted(books, key=lambda b: (b.get("name") or "").lower()):
                obj = album_obj(book)
                children.append({"id": obj["id"], "parent": mid, "isDir": True, "title": obj["name"], "artist": name, "coverArt": obj["coverArt"]})
            ok({"directory": {"id": mid, "name": name, "parent": "mf:0", "child": children}})
            return
        book, _idx = find_book(mid)
        if not book:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        songs = [song_obj(book, i, f) for i, f in enumerate(book.get("files") or [])]
        ok({"directory": {"id": album_key(book), "parent": artist_key(book_author(book)), "name": book.get("name") or "Untitled", "child": songs}})
        return
    if method == "getArtists":
        ok({"artists": {"index": grouped_artist_indexes(q1(qs, "musicFolderId"))}})
        return
    if method == "getArtist":
        aid = q1(qs, "id")
        if aid.startswith("mar:"):
            name = music_artist_name(aid)
            if not name:
                subsonic_error(handler, 70, "Not found", out_fmt)
                return
            albums = [music_album_obj(a) for a in sorted(
                [a for a in get_music_cached() if (a.get("artist") or "Unknown") == name],
                key=lambda a: (a.get("name") or "").lower()
            )]
            ok({"artist": {"id": music_artist_key(name), "name": name, "albumCount": len(albums), "album": albums}})
            return
        name = artist_name(aid)
        if not name:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        books = [b for b in folder_books("") if book_author(b) == name]
        albums = [album_obj(b) for b in sorted(books, key=lambda b: (b.get("name") or "").lower())]
        ok({"artist": {"id": artist_key(name), "name": name, "albumCount": len(albums), "album": albums}})
        return
    if method == "getAlbum":
        sid = q1(qs, "id")
        if sid.startswith("mal:") or sid.startswith("ms:"):
            album = music_album_by_id(sid)
            if not album:
                subsonic_error(handler, 70, "Not found", out_fmt)
                return
            ok({"album": music_album_obj(album, with_songs=True)})
            return
        book, _idx = find_book(sid)
        if not book:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        ok({"album": album_obj(book, with_songs=True)})
        return
    if method == "getSong":
        sid = q1(qs, "id")
        song = resolve_song(sid)
        if not song:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        ok({"song": song})
        return
    if method in ("getAlbumList", "getAlbumList2"):
        folder_id = q1(qs, "musicFolderId")
        kind = q1(qs, "type", "newest")
        pairs = listed_album_objs(folder_id)
        if kind == "starred":
            pairs = []
        elif kind == "recent":
            def recent_key(pair):
                kind_name, src, _obj = pair
                if kind_name == "book":
                    saved = (progress.get(username) or {}).get(src["id"]) or {}
                    return saved.get("updated") or 0
                try:
                    path = resolve_music(src.get("path") or "")
                    return int(path.stat().st_mtime * 1000) if path else 0
                except Exception:
                    return 0
            pairs = sorted(pairs, key=recent_key, reverse=True)
        elif kind == "alphabeticalByArtist":
            pairs = sorted(pairs, key=lambda p: ((p[2].get("artist") or "").lower(), (p[2].get("name") or "").lower()))
        elif kind == "alphabeticalByName":
            pairs = sorted(pairs, key=lambda p: (p[2].get("name") or "").lower())
        elif kind == "random":
            pairs = list(pairs)
            random.shuffle(pairs)
        else:
            pairs = sorted(pairs, key=lambda p: p[2].get("created") or "", reverse=True)
        albums = [p[2] for p in page_slice(pairs, qs)]
        key = "albumList2" if method == "getAlbumList2" else "albumList"
        ok({key: {"album": albums}})
        return
    if method == "getRandomSongs":
        songs = listed_songs(q1(qs, "musicFolderId"))
        random.shuffle(songs)
        try:
            size = int(q1(qs, "size", "10") or 10)
        except ValueError:
            size = 10
        ok({"randomSongs": {"song": songs[:size]}})
        return
    if method in ("search2", "search3"):
        query = q1(qs, "query").strip().strip('"').lower()
        folder_id = q1(qs, "musicFolderId")
        books = selected_books(folder_id)
        artists = []
        albums = []
        songs = []
        seen = set()
        if query:
            for book in books:
                author = book_author(book)
                mark = "ar:" + author
                if query in author.lower() and mark not in seen:
                    seen.add(mark)
                    count = sum(1 for b in books if book_author(b) == author)
                    artists.append({"id": artist_key(author), "name": author, "albumCount": count})
                if query in (book.get("name") or "").lower() or query in author.lower():
                    albums.append(album_obj(book))
                for i, info in enumerate(book.get("files") or []):
                    if query in (info.get("name") or "").lower() or query in (book.get("name") or "").lower():
                        songs.append(song_obj(book, i, info))
            for album in music_albums(folder_id):
                artist = album.get("artist") or "Unknown"
                mark = "mar:" + artist
                if query in artist.lower() and mark not in seen:
                    seen.add(mark)
                    count = sum(1 for a in music_albums(folder_id) if (a.get("artist") or "Unknown") == artist)
                    artists.append({"id": music_artist_key(artist), "name": artist, "albumCount": count})
                if query in (album.get("name") or "").lower() or query in artist.lower():
                    albums.append(music_album_obj(album))
                for info in album.get("files") or []:
                    hay = " ".join([
                        info.get("title") or "",
                        info.get("name") or "",
                        info.get("album") or "",
                        info.get("artist") or "",
                    ]).lower()
                    if query in hay:
                        songs.append(music_song_obj(album, info))
        def take(key, items):
            try:
                limit = int(q1(qs, key, "20") or 20)
            except ValueError:
                limit = 20
            return items[:limit]
        result = {"artist": take("artistCount", artists), "album": take("albumCount", albums), "song": take("songCount", songs)}
        ok({"searchResult3" if method == "search3" else "searchResult2": result})
        return
    if method in ("getStarred", "getStarred2"):
        key = "starred2" if method == "getStarred2" else "starred"
        ok({key: {"artist": [], "album": [], "song": []}})
        return
    if method in ("star", "unstar", "setRating"):
        ok()
        return
    if method == "scrobble":
        ids = qs.get("id") or []
        positions = qs.get("position") or []
        for i, sid in enumerate(ids):
            book, index = find_book(sid)
            if not book:
                continue
            if index is None:
                index = 0
            if i >= len(positions) or positions[i] == "":
                continue
            try:
                seconds = float(positions[i]) / 1000.0
            except ValueError:
                continue
            if seconds < 0 or seconds > 200000:
                continue
            set_listen_progress(username, book, index, seconds)
        ok()
        return
    if method == "getBookmarks":
        marks = []
        for book in folder_books(""):
            mark = progress_bookmark(username, book)
            if mark and mark["position"] > 0:
                marks.append(mark)
        ok({"bookmarks": {"bookmark": marks}})
        return
    if method == "createBookmark":
        book, index = find_book(q1(qs, "id"))
        if not book or index is None:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        try:
            seconds = float(q1(qs, "position", "0") or 0) / 1000.0
        except ValueError:
            seconds = 0
        set_listen_progress(username, book, index, seconds)
        ok()
        return
    if method == "deleteBookmark":
        book, _idx = find_book(q1(qs, "id"))
        if book and username in progress:
            progress[username].pop(book["id"], None)
            save_progress()
        ok()
        return
    if method == "savePlayQueue":
        ids = qs.get("id") or []
        current = q1(qs, "current") or (ids[0] if ids else "")
        try:
            position = int(float(q1(qs, "position", "0") or 0))
        except ValueError:
            position = 0
        playqueues[username] = {"ids": ids, "current": current, "position": position, "changed": iso_now()}
        save_playqueue()
        if current:
            book, index = find_book(current)
            if book and index is not None:
                set_listen_progress(username, book, index, position / 1000.0)
        ok()
        return
    if method == "getPlayQueue":
        q = playqueues.get(username) or {}
        entries = []
        for sid in q.get("ids") or []:
            song = resolve_song(sid)
            if song:
                entries.append(song)
        if not entries:
            ok({"playQueue": {"username": username, "position": 0, "changed": iso_now(), "entry": []}})
            return
        ok({"playQueue": {
            "current": q.get("current") or entries[0]["id"],
            "position": int(q.get("position") or 0),
            "username": username,
            "changed": q.get("changed") or iso_now(),
            "entry": entries,
        }})
        return
    if method in ("stream", "download"):
        sid = q1(qs, "id")
        if sid.startswith("ms:"):
            album, info = music_song_by_id(sid)
            if not album or not info:
                handler.send_error(404)
                return
            path = resolve_music(info.get("rel") or "")
            if path is None or not path.is_file():
                handler.send_error(404)
                return
            handler.stream_file(path, as_download=method == "download")
            return
        if sid.startswith("mal:"):
            album = music_album_by_id(sid)
            if not album:
                handler.send_error(404)
                return
            send_music_album_zip(handler, album)
            return
        book, index = find_book(sid)
        if not book:
            handler.send_error(404)
            return
        if index is None:
            send_book_zip(handler, book)
            return
        send_song_file(handler, book, index, method == "download")
        return
    if method == "getCoverArt":
        send_cover_art(handler, q1(qs, "id"))
        return
    if method == "getUser":
        asked = q1(qs, "username") or username
        if asked != username and username != "admin":
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        folders = list(range(len(library_roots())))
        if music_dir_ready():
            folders.append(MUSIC_FOLDER_ID)
        ok({"user": {
            "username": asked,
            "email": "",
            "scrobblingEnabled": True,
            "adminRole": asked == "admin",
            "settingsRole": asked == "admin",
            "downloadRole": True,
            "uploadRole": False,
            "playlistRole": True,
            "coverArtRole": False,
            "commentRole": False,
            "podcastRole": False,
            "streamRole": True,
            "jukeboxRole": False,
            "shareRole": False,
            "folder": folders,
        }})
        return
    if method == "getPlaylists":
        asked = q1(qs, "username")
        if asked:
            if asked != username and username != "admin":
                subsonic_error(handler, 50, "Not authorized", out_fmt)
                return
            items = [playlist_obj(pl) for pl in playlists.values() if pl.get("owner") == asked]
        else:
            items = [playlist_obj(pl) for pl in playlists.values() if playlist_visible(pl, username)]
        items.sort(key=lambda p: (p.get("name") or "").lower())
        ok({"playlists": {"playlist": items}})
        return
    if method == "getPlaylist":
        pid = q1(qs, "id") or q1(qs, "playlistId")
        if not pid:
            subsonic_error(handler, 10, "Required parameter missing", out_fmt)
            return
        pl = playlist_get(pid)
        if not pl:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        if not playlist_visible(pl, username):
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        ok({"playlist": playlist_obj(pl, with_entries=True)})
        return
    if method == "createPlaylist":
        pid = q1(qs, "playlistId")
        name = q1(qs, "name")
        song_ids = list(qs.get("songId") or [])
        if pid:
            pl = playlist_get(pid)
            if not pl:
                subsonic_error(handler, 70, "Not found", out_fmt)
                return
            if not playlist_owned(pl, username):
                subsonic_error(handler, 50, "Not authorized", out_fmt)
                return
            if name:
                pl["name"] = name
            pl["songIds"] = song_ids
            pl["changed"] = iso_now()
            save_playlists()
            ok({"playlist": playlist_obj(pl, with_entries=True)})
            return
        if not name:
            subsonic_error(handler, 10, "Required parameter missing", out_fmt)
            return
        pl = new_playlist(username, name, song_ids)
        ok({"playlist": playlist_obj(pl, with_entries=True)})
        return
    if method == "updatePlaylist":
        pid = q1(qs, "playlistId") or q1(qs, "id")
        if not pid:
            subsonic_error(handler, 10, "Required parameter missing", out_fmt)
            return
        pl = playlist_get(pid)
        if not pl:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        if not playlist_owned(pl, username):
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        name = q1(qs, "name")
        if name:
            pl["name"] = name
        if "comment" in qs:
            pl["comment"] = q1(qs, "comment")
        public = q_bool(qs, "public")
        if public is not None:
            pl["public"] = public
        song_ids = list(pl.get("songIds") or [])
        remove_idx = []
        for raw in qs.get("songIndexToRemove") or []:
            try:
                remove_idx.append(int(raw))
            except ValueError:
                continue
        for idx in sorted(set(remove_idx), reverse=True):
            if 0 <= idx < len(song_ids):
                song_ids.pop(idx)
        for sid in qs.get("songIdToAdd") or []:
            if sid:
                song_ids.append(sid)
        pl["songIds"] = song_ids
        pl["changed"] = iso_now()
        save_playlists()
        ok()
        return
    if method == "deletePlaylist":
        pid = q1(qs, "id") or q1(qs, "playlistId")
        if not pid:
            subsonic_error(handler, 10, "Required parameter missing", out_fmt)
            return
        pl = playlist_get(pid)
        if not pl:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        if not playlist_owned(pl, username):
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        playlists.pop(str(pl["id"]), None)
        save_playlists()
        ok()
        return
    if method == "getScanStatus":
        count = len(get_books_cached() or []) + len(get_music_cached() or [])
        ok({"scanStatus": {"scanning": False, "count": count}})
        return
    if method == "startScan":
        get_books_cached(force=True)
        get_music_cached(force=True)
        count = len(get_books_cached() or []) + len(get_music_cached() or [])
        ok({"scanStatus": {"scanning": False, "count": count}})
        return
    if method in ("getGenres", "getSongsByGenre", "getNowPlaying", "getVideos", "getPodcasts", "getNewestPodcasts", "getInternetRadioStations", "getShares", "getChatMessages", "getAvatar", "getLyrics", "getLyricsBySongId"):
        ok()
        return
    subsonic_error(handler, 70, "Unknown method", out_fmt)
