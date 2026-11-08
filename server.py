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
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "files" / "music"))
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
PLAYLISTS_FILE = Path(__file__).parent / "playlists.json"
COVER_CACHE = Path(__file__).parent / "covers"
COVER_CACHE.mkdir(exist_ok=True)
SESSIONS = {}
BOOKS_CACHE = {"data": None, "mtime": 0}
COVER_FETCH_LOCK = threading.Lock()
COVER_FETCHING = set()

# Register m4b MIME type (Python doesn't know it by default)
mimetypes.add_type("audio/mp4", ".m4b")
mimetypes.add_type("audio/mp4", ".m4a")

AUDIO_EXTS = {".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".webm"}
COVER_NAMES = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.jpeg",
               "folder.png", "album.jpg", "front.jpg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

def hash_password(password: str, salt: bytes = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return base64.b64encode(salt + dk).decode()

def verify_password(password: str, stored: str) -> bool:
    try:
        raw = base64.b64decode(stored)
        salt, dk = raw[:16], raw[16:]
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000) == dk
    except Exception:
        return False

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

if not USERS_FILE.exists():
    USERS_FILE.write_text(json.dumps({"admin": hash_password("changeme")}, indent=2))

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

progress = load_progress()

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

prefs = load_prefs()

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

collections = load_collections()

def load_finished():
    if FINISHED_FILE.exists():
        try:
            return json.loads(FINISHED_FILE.read_text())
        except Exception:
            pass
    return {}

def save_finished(data):
    FINISHED_FILE.write_text(json.dumps(data, indent=2))

finished = load_finished()

bookmarks = load_bookmarks()

def _is_auto_bookmark(mark):
    if mark.get("manual"):
        return False
    return bool(mark.get("auto")) or mark.get("note") == "auto"

metadata = load_metadata()

def save_progress():
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))

BACKUP_DIR = Path(__file__).parent / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
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
                print(f"Backup failed for {src.name}: {e}")
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
    print(f"Daily backup written to {BACKUP_DIR}")

daily_backup()

def parse_name(name: str):
    if " - " in name:
        parts = name.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return None, name

def fetch_from_isbn(isbn: str):
    """Look up a book by ISBN on Open Library, then Google Books."""
    isbn = isbn.replace("-", "").replace(" ", "").strip()
    if not isbn or not isbn.isdigit() or len(isbn) not in (10, 13):
        return None

    result = {"title": None, "author": None, "cover_url": None}

    # Direct cover-by-ISBN (works even if full record is missing)
    direct_cover = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    try:
        req = urllib.request.Request(direct_cover, method="HEAD")
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status == 200 and int(resp.headers.get("Content-Length", 0)) > 1000:
                result["cover_url"] = direct_cover
    except Exception:
        pass

    # Try Open Library ISBN endpoint for metadata
    try:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        result["title"] = data.get("title")
        if data.get("authors"):
            a = data["authors"][0]
            if isinstance(a, dict) and "name" in a:
                result["author"] = a["name"]
            elif isinstance(a, dict) and "key" in a:
                with urllib.request.urlopen(f"https://openlibrary.org{a['key']}.json", timeout=8) as aresp:
                    result["author"] = json.loads(aresp.read().decode()).get("name")
        if not result["cover_url"]:
            covers = data.get("covers") or []
            if covers:
                result["cover_url"] = f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg"
    except Exception as e:
        print(f"Open Library ISBN failed: {e}")

    # Fallback: Google Books if we still need title/author or cover
    if not result["title"] or not result["cover_url"]:
        try:
            url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode({
                "q": f"isbn:{isbn}", "maxResults": 1
            })
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("items") or []
            if items:
                info = items[0].get("volumeInfo", {})
                if not result["title"]:
                    result["title"] = info.get("title")
                if not result["author"]:
                    authors = info.get("authors") or []
                    result["author"] = authors[0] if authors else None
                if not result["cover_url"]:
                    image_links = info.get("imageLinks") or {}
                    cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
                    if cover_url:
                        result["cover_url"] = cover_url.replace("zoom=1", "zoom=0").replace("&edge=curl", "")
        except Exception as e:
            print(f"Google Books ISBN failed: {e}")

    if result["title"] or result["cover_url"]:
        return result
    return None
    """Extract metadata from a Google Books URL."""
    try:
        if "books.google" not in url and "googleapis.com/books" not in url:
            return None
        # Extract volume ID if present
        vol_id = None
        if "/books?id=" in url:
            vol_id = url.split("/books?id=")[1].split("&")[0]
        elif "id=" in url:
            vol_id = url.split("id=")[1].split("&")[0]
        if not vol_id:
            return None
        api_url = f"https://www.googleapis.com/books/v1/volumes/{vol_id}"
        with urllib.request.urlopen(api_url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        info = data.get("volumeInfo", {})
        title = info.get("title")
        authors = info.get("authors") or []
        author = authors[0] if authors else None
        image_links = info.get("imageLinks") or {}
        cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
        if cover_url:
            cover_url = cover_url.replace("zoom=1", "zoom=0").replace("&edge=curl", "")
        return {"title": title, "author": author, "cover_url": cover_url}
    except Exception as e:
        print(f"Google Books URL fetch failed: {e}")
        return None
    """Try to extract title, author, and cover from an Open Library URL."""
    try:
        # Normalize and extract OLID
        if "openlibrary.org" not in url:
            return None
        olid = None
        if "/works/" in url:
            olid = url.split("/works/")[1].split("/")[0].split("?")[0]
            api_url = f"https://openlibrary.org/works/{olid}.json"
        elif "/books/" in url:
            olid = url.split("/books/")[1].split("/")[0].split("?")[0]
            api_url = f"https://openlibrary.org/books/{olid}.json"
        else:
            return None

        with urllib.request.urlopen(api_url, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        title = data.get("title")
        author = None
        if data.get("authors"):
            # Works have author keys; books may have author_name or need another call
            if isinstance(data["authors"][0], dict) and "name" in data["authors"][0]:
                author = data["authors"][0]["name"]
            elif isinstance(data["authors"][0], dict) and "author" in data["authors"][0]:
                # Follow author key
                akey = data["authors"][0]["author"]["key"]
                with urllib.request.urlopen(f"https://openlibrary.org{akey}.json", timeout=8) as aresp:
                    adata = json.loads(aresp.read().decode())
                    author = adata.get("name")

        cover_url = None
        covers = data.get("covers") or []
        if covers:
            cover_url = f"https://covers.openlibrary.org/b/id/{covers[0]}-L.jpg"

        return {"title": title, "author": author, "cover_url": cover_url}
    except Exception as e:
        print(f"Open Library fetch failed: {e}")
        return None

def fetch_cover(author, title, book_id):
    cache_path = COVER_CACHE / f"{book_id}.jpg"
    if cache_path.exists():
        return cache_path
    if not title:
        return None

    # Try Open Library
    query = title
    if author:
        query = f"{title} {author}"
    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode({
        "q": query, "limit": 1, "fields": "cover_i,title,author_name"
    })
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        docs = data.get("docs") or []
        if docs and docs[0].get("cover_i"):
            cover_id = docs[0]["cover_i"]
            img_url = f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
            urllib.request.urlretrieve(img_url, cache_path)
            if cache_path.stat().st_size > 1000:
                return cache_path
            cache_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"Open Library cover failed for {title}: {e}")

    # Fallback: Google Books
    try:
        gb_query = title
        if author:
            gb_query = f"intitle:{title} inauthor:{author}"
        gb_url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode({
            "q": gb_query, "maxResults": 1
        })
        with urllib.request.urlopen(gb_url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("items") or []
        if items:
            info = items[0].get("volumeInfo", {})
            image_links = info.get("imageLinks") or {}
            img_url = image_links.get("thumbnail") or image_links.get("smallThumbnail")
            if img_url:
                img_url = img_url.replace("zoom=1", "zoom=0").replace("&edge=curl", "")
                urllib.request.urlretrieve(img_url, cache_path)
                if cache_path.stat().st_size > 1000:
                    return cache_path
                cache_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"Google Books cover failed for {title}: {e}")

    return None

def get_book_id(rel_path: str) -> str:
    return base64.urlsafe_b64encode(rel_path.encode()).decode().rstrip("=")

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

def _resolved_dir(raw):
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return Path(raw).expanduser()

def is_music_path(path):
    return _resolved_dir(path) == _resolved_dir(MUSIC_DIR)

def library_roots():
    roots = []
    seen = set()
    candidates = [str(AUDIO_DIR)] + load_extra_libraries()
    music = _resolved_dir(MUSIC_DIR)
    # Only attach a real music folder, and never shift existing audiobook indexes.
    if music.is_dir():
        candidates.append(str(music))
    for raw in candidates:
        p = _resolved_dir(raw)
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        roots.append(p)
    return roots or [AUDIO_DIR]

def split_lib_path(rel: str):
    rel = (rel or "").replace("\\", "/").strip("/")
    if len(rel) >= 3 and rel[0] == "~":
        end = rel.find("~", 1)
        if end > 1 and rel[1:end].isdigit():
            return int(rel[1:end]), rel[end + 1:].lstrip("/")
    return 0, rel

def join_lib_path(index: int, inner: str) -> str:
    inner = (inner or "").strip("/")
    if index == 0:
        return inner
    return f"~{index}~/{inner}" if inner else f"~{index}~"

def resolve_lib(rel: str):
    """Return (absolute path, root, index) or (None, None, None) if outside a library."""
    index, inner = split_lib_path(rel)
    if ".." in Path(inner).parts:
        return None, None, None
    roots = library_roots()
    if index < 0 or index >= len(roots):
        return None, None, None
    root = roots[index]
    try:
        root_res = root.resolve()
        target = (root_res / inner).resolve() if inner else root_res
    except Exception:
        return None, None, None
    if target != root_res and root_res not in target.parents:
        return None, None, None
    return target, root_res, index

def library_list():
    items = []
    for i, root in enumerate(library_roots()):
        if is_music_path(root):
            continue
        items.append({
            "path": str(root),
            "name": root.name or str(root),
            "exists": root.is_dir(),
            "removable": i != 0,
        })
    return items

def scan_folder(rel: str = ""):
    """Return folders and books at a given relative path."""
    roots = library_roots()
    book_indexes = [i for i, root in enumerate(roots) if not is_music_path(root)]
    if not (rel or "").strip("/") and len(book_indexes) > 1:
        folders = [{"name": (roots[i].name or str(roots[i])), "path": f"~{i}~"} for i in book_indexes]
        return {"folders": folders, "books": [], "current": "", "rootName": "Libraries"}

    target, root, index = resolve_lib(rel)
    if target is None or not target.is_dir() or is_music_path(root):
        return {"folders": [], "books": [], "current": rel, "rootName": ""}
    _, inner = split_lib_path(rel)

    folders = []
    books = []
    files_in_this_dir = []
    local_cover = None
    local_meta = None

    try:
        for entry in sorted(target.iterdir()):
            if entry.name.startswith("."):
                continue
            child_inner = f"{inner}/{entry.name}" if inner else entry.name
            entry_rel = join_lib_path(index, child_inner)
            if entry.is_dir():
                folders.append({"name": entry.name, "path": entry_rel})
            elif entry.suffix.lower() in AUDIO_EXTS:
                try:
                    files_in_this_dir.append({"name": entry.name, "rel": entry_rel, "size": entry.stat().st_size})
                except Exception:
                    continue
            elif entry.suffix.lower() in IMAGE_EXTS:
                if entry.name.lower() in COVER_NAMES:
                    local_cover = entry_rel
                elif local_cover is None:
                    local_cover = entry_rel
            elif entry.name.lower() == "metadata.json":
                local_meta = entry_rel
    except Exception as e:
        print(f"Scan error: {e}")

    if files_in_this_dir:
        files_in_this_dir.sort(key=lambda f: f["name"])
        folder_name = Path(inner).name if inner else (root.name or "Library")
        author, title = parse_name(folder_name)

        if local_meta:
            try:
                meta_file, _, _ = resolve_lib(local_meta)
                local_data = json.loads(meta_file.read_text())
                if local_data.get("title"):
                    title = local_data["title"]
                if local_data.get("author") or local_data.get("authors"):
                    author = local_data.get("author") or (local_data.get("authors") or [None])[0]
            except Exception as e:
                print(f"Local metadata read failed: {e}")

        book_key = join_lib_path(index, inner) or files_in_this_dir[0]["rel"]
        book_id = get_book_id(book_key)
        custom = metadata.get(book_id, {})
        books.append({
            "id": book_id,
            "name": custom.get("title") or title or folder_name,
            "author": custom.get("author") or author or "",
            "files": files_in_this_dir,
            "cover": local_cover,
            "path": join_lib_path(index, inner)
        })

    return {"folders": folders, "books": books, "current": rel, "rootName": root.name or ""}

def scan_books_full():
    """Full scan used by book page and streaming."""
    books = []

    def walk(path: Path, index: int, inner: str = ""):
        try:
            if not path.exists():
                return
            files = []
            cover = None
            local_meta = None
            for entry in sorted(path.iterdir()):
                if entry.name.startswith("."):
                    continue
                child_inner = f"{inner}/{entry.name}" if inner else entry.name
                rel = join_lib_path(index, child_inner)
                if entry.is_dir():
                    walk(entry, index, child_inner)
                elif entry.suffix.lower() in AUDIO_EXTS:
                    try:
                        files.append({"name": entry.name, "rel": rel, "size": entry.stat().st_size})
                    except Exception:
                        continue
                elif entry.suffix.lower() in IMAGE_EXTS:
                    if entry.name.lower() in COVER_NAMES:
                        cover = rel
                    elif cover is None:
                        cover = rel
                elif entry.name.lower() == "metadata.json":
                    local_meta = rel
            if files:
                files.sort(key=lambda f: f["name"])
                folder_name = Path(inner).name if inner else path.name
                author, title = parse_name(folder_name)
                if inner and "/" in inner:
                    parent = Path(inner).parent.name
                    if parent and not author:
                        author = parent
                if local_meta:
                    try:
                        meta_file, _, _ = resolve_lib(local_meta)
                        local_data = json.loads(meta_file.read_text())
                        if local_data.get("title"):
                            title = local_data["title"]
                        if local_data.get("author") or local_data.get("authors"):
                            author = local_data.get("author") or (local_data.get("authors") or [None])[0]
                    except Exception as e:
                        print(f"Local metadata read failed: {e}")
                book_key = (inner if index == 0 else join_lib_path(index, inner)) or files[0]["rel"]
                book_id = get_book_id(book_key)
                custom = metadata.get(book_id, {})
                books.append({
                    "id": book_id,
                    "name": custom.get("title") or title or folder_name,
                    "author": custom.get("author") or author or "",
                    "files": files,
                    "cover": cover,
                    "path": join_lib_path(index, inner),
                    "totalSize": sum(f["size"] for f in files),
                    "kind": "music" if is_music_path(library_roots()[index]) else "audiobook",
                })
        except Exception as e:
            print(f"Walk error: {e}")

    for i, root in enumerate(library_roots()):
        walk(root, i, "")
    return books

def get_book_by_id(book_id: str):
    """Fast lookup for a single book by decoding its ID to a path."""
    try:
        padded = book_id + "=" * ((4 - len(book_id) % 4) % 4)
        rel = base64.urlsafe_b64decode(padded).decode()
    except Exception:
        return None
    if ".." in Path(rel).parts:
        return None

    cached = BOOKS_CACHE.get("data")
    if cached:
        found = next((b for b in cached if b["id"] == book_id), None)
        if found:
            return found

    target, root, index = resolve_lib(rel)
    if target is None or not target.is_dir():
        books = get_books_cached()
        return next((b for b in books if b["id"] == book_id), None)

    _, inner = split_lib_path(rel)
    files = []
    cover = None
    local_meta = None
    try:
        for entry in sorted(target.iterdir()):
            if entry.name.startswith("."):
                continue
            child_inner = f"{inner}/{entry.name}" if inner else entry.name
            entry_rel = join_lib_path(index, child_inner)
            if entry.suffix.lower() in AUDIO_EXTS:
                try:
                    files.append({"name": entry.name, "rel": entry_rel, "size": entry.stat().st_size})
                except Exception:
                    continue
            elif entry.suffix.lower() in IMAGE_EXTS:
                if cover is None or entry.name.lower() in COVER_NAMES:
                    cover = entry_rel
            elif entry.name.lower() == "metadata.json":
                local_meta = entry_rel
    except Exception:
        pass

    if not files:
        return None

    files.sort(key=lambda f: f["name"])
    folder_name = Path(inner).name if inner else (root.name if root else "Library")
    author, title = parse_name(folder_name)
    if local_meta:
        try:
            meta_file, _, _ = resolve_lib(local_meta)
            local_data = json.loads(meta_file.read_text())
            if local_data.get("title"):
                title = local_data["title"]
            if local_data.get("author") or local_data.get("authors"):
                author = local_data.get("author") or (local_data.get("authors") or [None])[0]
        except Exception:
            pass

    custom = metadata.get(book_id, {})
    return {
        "id": book_id,
        "name": custom.get("title") or title or folder_name,
        "author": custom.get("author") or author or "",
        "files": files,
        "cover": cover,
        "path": join_lib_path(index, inner),
        "totalSize": sum(f["size"] for f in files),
        "kind": "music" if is_music_path(root) else "audiobook",
    }

def get_books_cached(force=False):
    """Return full book list. Only rescans when force=True or cache is empty."""
    current_mtime = 0
    for root in library_roots():
        try:
            current_mtime += root.stat().st_mtime
        except Exception:
            pass
    try:
        current_mtime += LIBRARIES_FILE.stat().st_mtime
    except Exception:
        pass
    if force or BOOKS_CACHE["data"] is None or current_mtime != BOOKS_CACHE.get("mtime", 0):
        BOOKS_CACHE["data"] = scan_books_full()
        BOOKS_CACHE["mtime"] = current_mtime
        BOOKS_CACHE["ts"] = time.time()
    return BOOKS_CACHE["data"]

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
        path = Path(__file__).parent / "https-url.txt"
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    url = line
                    break
    if not url:
        url = "https://audobooks.covertsa.com"
    if url and not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url.rstrip("/")

HTTPS_REDIRECT = load_https_redirect()

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

SUBSONIC_VERSION = "1.16.1"

def load_playqueue():
    if PLAYQUEUE_FILE.exists():
        try:
            data = json.loads(PLAYQUEUE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}

playqueues = load_playqueue()

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

playlists = load_playlists()

def save_playlists():
    PLAYLISTS_FILE.write_text(json.dumps(playlists, indent=2))

def playlist_visible(pl, username):
    if not pl:
        return False
    if pl.get("owner") == username:
        return True
    return bool(pl.get("public"))

def playlist_songs(pl):
    songs = []
    for sid in pl.get("songIds") or []:
        book, index = find_book(sid)
        files = (book or {}).get("files") or []
        if book and index is not None and 0 <= index < len(files):
            songs.append(song_obj(book, index, files[index]))
    return songs

def playlist_obj(pl, username, with_entries=False):
    songs = playlist_songs(pl)
    obj = {
        "id": pl["id"],
        "name": pl.get("name") or "Playlist",
        "comment": pl.get("comment") or "",
        "owner": pl.get("owner") or "",
        "public": bool(pl.get("public")),
        "songCount": len(songs),
        "duration": sum(s.get("duration") or 0 for s in songs),
        "created": pl.get("created") or iso_now(),
        "changed": pl.get("changed") or iso_now(),
    }
    if songs:
        obj["coverArt"] = songs[0]["coverArt"]
    if with_entries:
        obj["entry"] = songs
    return obj

def iso_now(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))

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

def q1(qs, key, default=""):
    vals = qs.get(key) or []
    return vals[0] if vals else default

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

def book_root_index(book):
    rel = book.get("path") or ""
    if not rel and book.get("files"):
        rel = book["files"][0].get("rel") or ""
    index, _inner = split_lib_path(rel)
    return index

def folder_books(folder_id=""):
    books = list(get_books_cached() or [])
    if folder_id.startswith("mf:"):
        try:
            want = int(folder_id[3:])
        except ValueError:
            return books
        books = [b for b in books if book_root_index(b) == want]
    return books

def book_author(book):
    return book.get("author") or "Unknown"

def guess_duration(size):
    if not size:
        return 0
    return max(1, int(int(size) * 8 / 128000))

def book_created(book):
    files = book.get("files") or []
    if files:
        path, _root, _idx = resolve_lib(files[0].get("rel") or "")
        if path and path.exists():
            try:
                return iso_now(path.stat().st_mtime)
            except Exception:
                pass
    return iso_now()

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
    genre = "Music" if book.get("kind") == "music" else "Audiobook"
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
        "genre": genre,
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
        "genre": "Music" if book.get("kind") == "music" else "Audiobook",
    }
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

def cover_file(book):
    book_id = book["id"]
    custom_meta = metadata.get(book_id, {})
    cached = COVER_CACHE / f"{book_id}.jpg"
    if custom_meta.get("cover_url") and cached.exists():
        return cached
    if book.get("cover"):
        path, _root, _idx = resolve_lib(book["cover"])
        if path and path.is_file():
            return path
    if cached.exists():
        return cached
    return None

def artists_from(books):
    grouped = {}
    for book in books:
        grouped.setdefault(book_author(book), []).append(book)
    return grouped

def sort_key_name(name):
    low = (name or "").strip().lower()
    for prefix in ("the ", "a ", "an "):
        if low.startswith(prefix):
            return (name or "")[len(prefix):]
    return name or ""

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
    if not entries:
        handler.send_error(404)
        return
    names = []
    used = set()
    packed = []
    for name, path, size in entries:
        arc = name.replace("\\", "/").split("/")[-1] or "audio"
        base = arc
        n = 2
        while arc in used:
            stem = Path(base).stem
            ext = Path(base).suffix
            arc = f"{stem}-{n}{ext}"
            n += 1
        used.add(arc)
        names.append(arc.encode())
        packed.append((arc, path, size))
    total = 22
    for arc, _path, size in packed:
        nlen = len(arc.encode())
        total += 30 + nlen + size + 16 + 46 + nlen
    title = (book.get("name") or "book").replace('"', "")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header("Content-Length", str(total))
    handler.send_header("Content-Disposition", f'attachment; filename="{title}.zip"')
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
        crc = 0
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
        ok({"musicFolders": {"musicFolder": folders}})
        return
    if method == "getIndexes":
        books = folder_books(q1(qs, "musicFolderId"))
        grouped = artists_from(books)
        letters = {}
        for name, items in grouped.items():
            label = sort_key_name(name).strip()[:1].upper()
            if not label.isalpha():
                label = "#"
            letters.setdefault(label, []).append((name, items))
        indexes = []
        for label in sorted(letters):
            artists = []
            for name, items in sorted(letters[label], key=lambda pair: sort_key_name(pair[0]).lower()):
                artists.append({"id": artist_key(name), "name": name, "albumCount": len(items)})
            indexes.append({"name": label, "artist": artists})
        ok({"indexes": {"lastModified": int(time.time() * 1000), "index": indexes}})
        return
    if method == "getMusicDirectory":
        mid = q1(qs, "id")
        if mid.startswith("mf:"):
            books = folder_books(mid)
            children = []
            for name in sorted(artists_from(books), key=lambda n: sort_key_name(n).lower()):
                children.append({"id": artist_key(name), "isDir": True, "title": name, "artist": name})
            ok({"directory": {"id": mid, "name": "Library", "child": children}})
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
        books = folder_books(q1(qs, "musicFolderId"))
        grouped = artists_from(books)
        letters = {}
        for name, items in grouped.items():
            label = sort_key_name(name).strip()[:1].upper() or "#"
            if not label.isalpha():
                label = "#"
            letters.setdefault(label, []).append({"id": artist_key(name), "name": name, "albumCount": len(items)})
        indexes = [{"name": label, "artist": sorted(letters[label], key=lambda a: a["name"].lower())} for label in sorted(letters)]
        ok({"artists": {"index": indexes}})
        return
    if method == "getArtist":
        name = artist_name(q1(qs, "id"))
        if not name:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        books = [b for b in folder_books("") if book_author(b) == name]
        albums = [album_obj(b) for b in sorted(books, key=lambda b: (b.get("name") or "").lower())]
        ok({"artist": {"id": artist_key(name), "name": name, "albumCount": len(albums), "album": albums}})
        return
    if method == "getAlbum":
        book, _idx = find_book(q1(qs, "id"))
        if not book:
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        ok({"album": album_obj(book, with_songs=True)})
        return
    if method == "getSong":
        book, index = find_book(q1(qs, "id"))
        if not book or index is None or index >= len(book.get("files") or []):
            subsonic_error(handler, 70, "Not found", out_fmt)
            return
        ok({"song": song_obj(book, index, book["files"][index])})
        return
    if method in ("getAlbumList", "getAlbumList2"):
        books = folder_books(q1(qs, "musicFolderId"))
        kind = q1(qs, "type", "newest")
        if kind == "recent":
            def recent_key(book):
                saved = (progress.get(username) or {}).get(book["id"]) or {}
                return saved.get("updated") or 0
            books = sorted(books, key=recent_key, reverse=True)
        elif kind == "alphabeticalByArtist":
            books = sorted(books, key=lambda b: (book_author(b).lower(), (b.get("name") or "").lower()))
        elif kind == "alphabeticalByName":
            books = sorted(books, key=lambda b: (b.get("name") or "").lower())
        elif kind == "random":
            books = list(books)
            random.shuffle(books)
        elif kind == "starred":
            books = []
        else:
            books = sorted(books, key=book_created, reverse=True)
        albums = [album_obj(b) for b in page_slice(books, qs)]
        key = "albumList2" if method == "getAlbumList2" else "albumList"
        ok({key: {"album": albums}})
        return
    if method == "getRandomSongs":
        songs = []
        for book in folder_books(q1(qs, "musicFolderId")):
            for i, info in enumerate(book.get("files") or []):
                songs.append(song_obj(book, i, info))
        random.shuffle(songs)
        try:
            size = int(q1(qs, "size", "10") or 10)
        except ValueError:
            size = 10
        ok({"randomSongs": {"song": songs[:size]}})
        return
    if method in ("search2", "search3"):
        query = q1(qs, "query").strip().strip('"').lower()
        books = folder_books(q1(qs, "musicFolderId"))
        artists = []
        albums = []
        songs = []
        seen = set()
        if query:
            for book in books:
                author = book_author(book)
                if query in author.lower() and author not in seen:
                    seen.add(author)
                    count = sum(1 for b in books if book_author(b) == author)
                    artists.append({"id": artist_key(author), "name": author, "albumCount": count})
                if query in (book.get("name") or "").lower() or query in author.lower():
                    albums.append(album_obj(book))
                for i, info in enumerate(book.get("files") or []):
                    if query in (info.get("name") or "").lower() or query in (book.get("name") or "").lower():
                        songs.append(song_obj(book, i, info))
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
        times = qs.get("time") or []
        for i, sid in enumerate(ids):
            book, index = find_book(sid)
            if not book or index is None:
                continue
            if i < len(times) and times[i] != "":
                try:
                    seconds = float(times[i])
                except ValueError:
                    seconds = 0
            else:
                seconds = (progress.get(username) or {}).get(book["id"], {}).get("time", 0)
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
            book, index = find_book(sid)
            if book and index is not None and index < len(book.get("files") or []):
                entries.append(song_obj(book, index, book["files"][index]))
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
        book, _idx = find_book(q1(qs, "id"))
        if not book:
            handler.send_error(404)
            return
        path = cover_file(book)
        if not path:
            handler.send_error(404)
            return
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        data = path.read_bytes()
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "public, max-age=86400")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(data)
        return
    if method == "getUser":
        asked = q1(qs, "username") or username
        if asked != username and username != "admin":
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        folders = list(range(len(library_roots())))
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
        if asked and asked != username and username != "admin":
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        visible = []
        for pl in playlists.values():
            if asked and pl.get("owner") != asked:
                continue
            if playlist_visible(pl, username):
                visible.append(playlist_obj(pl, username))
        visible.sort(key=lambda item: (item.get("name") or "").lower())
        ok({"playlists": {"playlist": visible}})
        return
    if method == "getPlaylist":
        pid = q1(qs, "id")
        pl = playlists.get(pid)
        if not pl or not playlist_visible(pl, username):
            subsonic_error(handler, 70, "Playlist not found", out_fmt)
            return
        ok({"playlist": playlist_obj(pl, username, with_entries=True)})
        return
    if method == "createPlaylist":
        song_ids = qs.get("songId") or []
        if isinstance(song_ids, str):
            song_ids = [song_ids]
        existing_id = q1(qs, "playlistId")
        now = iso_now()
        if existing_id:
            pl = playlists.get(existing_id)
            if not pl:
                subsonic_error(handler, 70, "Playlist not found", out_fmt)
                return
            if pl.get("owner") != username and username != "admin":
                subsonic_error(handler, 50, "Not authorized", out_fmt)
                return
            if q1(qs, "name"):
                pl["name"] = q1(qs, "name")
            if "songId" in qs:
                pl["songIds"] = list(song_ids)
            pl["changed"] = now
            save_playlists()
            ok({"playlist": playlist_obj(pl, username, with_entries=True)})
            return
        name = q1(qs, "name") or "Playlist"
        pid = "pl_" + secrets.token_hex(8)
        pl = {
            "id": pid,
            "name": name,
            "owner": username,
            "public": False,
            "comment": q1(qs, "comment") or "",
            "created": now,
            "changed": now,
            "songIds": list(song_ids),
        }
        playlists[pid] = pl
        save_playlists()
        ok({"playlist": playlist_obj(pl, username, with_entries=True)})
        return
    if method == "updatePlaylist":
        pid = q1(qs, "playlistId") or q1(qs, "id")
        pl = playlists.get(pid)
        if not pl:
            subsonic_error(handler, 70, "Playlist not found", out_fmt)
            return
        if pl.get("owner") != username and username != "admin":
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        if q1(qs, "name"):
            pl["name"] = q1(qs, "name")
        if "comment" in qs:
            pl["comment"] = q1(qs, "comment") or ""
        if "public" in qs:
            pl["public"] = q1(qs, "public", "").lower() in ("true", "1", "yes")
        to_add = qs.get("songIdToAdd") or []
        if isinstance(to_add, str):
            to_add = [to_add]
        if to_add:
            pl.setdefault("songIds", []).extend(to_add)
        to_remove = qs.get("songIndexToRemove") or []
        if isinstance(to_remove, str):
            to_remove = [to_remove]
        if to_remove:
            # Indexes match the entries returned by getPlaylist (resolved songs only).
            resolved_at = []
            for n, sid in enumerate(pl.get("songIds") or []):
                book, index = find_book(sid)
                files = (book or {}).get("files") or []
                if book and index is not None and 0 <= index < len(files):
                    resolved_at.append(n)
            drop = set()
            for raw in to_remove:
                if not str(raw).isdigit():
                    continue
                i = int(raw)
                if 0 <= i < len(resolved_at):
                    drop.add(resolved_at[i])
            pl["songIds"] = [sid for n, sid in enumerate(pl.get("songIds") or []) if n not in drop]
        pl["changed"] = iso_now()
        save_playlists()
        ok()
        return
    if method == "deletePlaylist":
        pid = q1(qs, "id")
        pl = playlists.get(pid)
        if not pl:
            subsonic_error(handler, 70, "Playlist not found", out_fmt)
            return
        if pl.get("owner") != username and username != "admin":
            subsonic_error(handler, 50, "Not authorized", out_fmt)
            return
        playlists.pop(pid, None)
        save_playlists()
        ok()
        return
    if method == "getScanStatus":
        ok({"scanStatus": {"scanning": False, "count": len(get_books_cached() or [])}})
        return
    if method == "startScan":
        get_books_cached(force=True)
        ok({"scanStatus": {"scanning": False, "count": len(get_books_cached() or [])}})
        return
    if method in ("getGenres", "getSongsByGenre", "getNowPlaying", "getVideos", "getPodcasts", "getNewestPodcasts", "getInternetRadioStations", "getShares", "getChatMessages", "getAvatar", "getLyrics", "getLyricsBySongId"):
        ok()
        return
    subsonic_error(handler, 70, "Unknown method", out_fmt)

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
                book_id = path[6:]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                html = BOOK_HTML.replace("{{BOOK_ID}}", book_id)
                self.wfile.write(html.encode())
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

            if path == "/api/libraries":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(library_list()).encode())
                return

            if path == "/api/browse":
                qs = parse_qs(parsed.query)
                rel = qs.get("path", [""])[0]
                # Basic path safety
                if ".." in rel or rel.startswith("/"):
                    rel = ""
                data = scan_folder(rel)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
                return

            if path == "/api/rescan":
                get_books_cached(force=True)
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
                    print("Clear covers error:", e)
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
                books = [b for b in (get_books_cached() or []) if b.get("kind") != "music"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(books).encode())
                return

            if path.startswith("/api/book/"):
                book_id = path[10:]
                print(f"Book lookup: {book_id}")
                book = get_book_by_id(book_id)
                if not book or book.get("kind") == "music":
                    print(f"Book not found: {book_id}")
                    self.send_error(404)
                    return
                print(f"Book found: {book.get('name')} ({len(book.get('files', []))} files)")
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
                parts = path[8:].split("/")
                book_id = parts[0]
                try:
                    file_index = int(parts[1]) if len(parts) > 1 else 0
                except ValueError:
                    file_index = 0
                qs = parse_qs(parsed.query)
                as_download = qs.get("download", ["0"])[0] == "1"
                print(f"Stream request: {book_id} file {file_index}")
                book = get_book_by_id(book_id)
                if not book or file_index >= len(book["files"]):
                    print(f"Stream failed: book not found or bad index")
                    self.send_error(404)
                    return
                file_path, _, _ = resolve_lib(book["files"][file_index]["rel"])
                if file_path is None:
                    self.send_error(403)
                    return
                if not file_path.is_file():
                    print(f"Stream failed: file missing {file_path}")
                    self.send_error(404)
                    return
                print(f"Streaming: {file_path}")
                self.stream_file(file_path, as_download=as_download)
                return

            if path.startswith("/cover/"):
                book_id = path[7:]
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
                    cover_path, _, _ = resolve_lib(book["cover"])
                    if cover_path and cover_path.is_file():
                        mime = "image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg"
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.end_headers()
                        with open(cover_path, "rb") as f:
                            self.wfile.write(f.read())
                        return

                # Serve already-cached auto cover if present
                cached = COVER_CACHE / f"{book_id}.jpg"
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
            print("GET error:", traceback.format_exc())
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
                    remember_password(user, passwd)
                    token = secrets.token_urlsafe(32)
                    SESSIONS[token] = {"created": time.time(), "user": user}
                    self.send_response(302)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", session_cookie(self, token, 604800))
                    self.end_headers()
                else:
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
                            print(f"Cover download failed: {e}")
                    metadata[book_id] = meta
                    save_metadata(metadata)
                    # Invalidate book cache so changes show immediately
                    BOOKS_CACHE["data"] = None
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except Exception as e:
                    print("set-meta error:", e)
                    self.send_error(400)
                return

            self.send_error(404)
        except Exception as e:
            print("POST error:", traceback.format_exc())
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

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

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
  #continue { padding: 1.25rem 1.75rem 0; }
  #continue h3 { margin: 0 0 0.6rem; font-size: 0.95rem; font-weight: 600; color: var(--muted); }
  #continue-list .item { min-width: 200px; }
  .error { color: #f66; padding: 1rem; }
</style>
</head>
<body>
<div style="display:flex; min-height:100vh;">
  <aside style="width:220px; background:var(--card); border-right:1px solid var(--border); padding:1.25rem 1rem; flex-shrink:0;">
    <h1 style="margin:0 0 1.5rem; font-size:1.15rem;">Audiobooks</h1>
    <div id="breadcrumb" style="font-size:0.85rem; margin-bottom:1.5rem;"></div>
    <div style="display:flex; flex-direction:column; gap:0.4rem;">
      <button onclick="showCollections()" style="text-align:left;">Collections</button>
      <button onclick="showFinished()" style="text-align:left;">Finished</button>
      <button onclick="rescan()" style="text-align:left;">Rescan</button>
      <button onclick="clearCovers()" style="text-align:left;">Clear Covers</button>
      <button onclick="toggleTheme()" style="text-align:left;">Theme</button>
      <a href="/password"><button style="width:100%; text-align:left;">Password</button></a>
      <a id="libraries-link" href="#" onclick="showLibraries(); return false;" style="display:none"><button style="width:100%; text-align:left;">Libraries</button></a>
      <a id="users-link" href="/users" style="display:none"><button style="width:100%; text-align:left;">Users</button></a>
      <a href="/logout"><button style="width:100%; text-align:left;">Logout</button></a>
    </div>
  </aside>
  <div style="flex:1; display:flex; flex-direction:column;">
    <header style="padding:0.9rem 1.5rem; background:var(--card); border-bottom:1px solid var(--border); display:flex; align-items:center; gap:1rem;">
      <input id="global-search" placeholder="Search books..." style="flex:1; max-width:320px; padding:0.5rem 0.75rem; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--fg);" oninput="globalSearch()">
      <button id="view-toggle" onclick="toggleView()" style="white-space:nowrap;">All Books</button>
    </header>
    <div id="continue" style="padding:1.25rem 1.5rem 0; display:none;">
      <h3 style="margin:0 0 0.6rem; font-size:0.9rem; color:var(--muted);">Continue Listening</h3>
      <div id="continue-list" style="display:flex; gap:0.75rem; overflow-x:auto; padding-bottom:0.5rem;"></div>
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

let currentPath = '';
let allBooksCache = null;
let viewMode = 'folders'; // 'folders' or 'books'
let finishedIds = new Set();

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
        ${extra || ''}
      </div>
    </div>`;
}

function toggleView() {
  viewMode = viewMode === 'folders' ? 'books' : 'folders';
  document.getElementById('view-toggle').textContent = viewMode === 'folders' ? 'All Books' : 'Folders';
  if (viewMode === 'books') {
    showAllBooks();
  } else {
    load(currentPath);
  }
}

async function showAllBooks() {
  const books = (await loadAllBooks()).filter(b => !finishedIds.has(b.id));
  document.getElementById('breadcrumb').innerHTML = 'All Books';
  document.getElementById('continue').style.display = '';
  showContinue();
  document.getElementById('list').innerHTML = books.length ? books.map(b => bookCard(b)).join('') : '<p>No books found.</p>';
}

async function loadAllBooks() {
  if (!allBooksCache) {
    const r = await fetch('/api/books');
    allBooksCache = await r.json();
  }
  return allBooksCache;
}

async function showCollections() {
  const r = await fetch('/api/collections');
  const cols = await r.json();
  const names = Object.keys(cols);
  let html = '<div style="padding:1.5rem;"><h3>Collections</h3>';
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
  document.getElementById('list').innerHTML = html;
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
  document.getElementById('list').innerHTML = booksInCol.length ? booksInCol.map(b => bookCard(b)).join('') : '<p>Empty collection.</p>';
}

async function showFinished() {
  await loadFinished();
  const books = await loadAllBooks();
  const done = books.filter(b => finishedIds.has(b.id));
  document.getElementById('breadcrumb').innerHTML = '<a href="#" onclick="load(\\'\\'); return false;">Library</a> / Finished';
  document.getElementById('continue').style.display = 'none';
  document.getElementById('list').innerHTML = done.length ? done.map(b => bookCard(b,
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
      <div class="item" style="min-width:200px; flex-shrink:0; padding:0.6rem; position:relative;" onclick="location.href='/book/${x.id}'">
        <div class="item-name">${x.book.name || 'Untitled'}</div>
        ${folder ? `<div class="author" style="font-size:0.75rem; opacity:0.7;">${folder}</div>` : ''}
        <button onclick="event.stopPropagation(); removeContinue('${x.id}')" style="position:absolute; top:4px; right:4px; padding:0.15rem 0.4rem; font-size:0.75rem; background:transparent; border:none; color:var(--muted);">×</button>
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
      (b.author || '').toLowerCase().includes(q)
    )
  );
  document.getElementById('list').innerHTML = matches.length ? matches.map(b => bookCard(b)).join('') : '<p>No matches</p>';
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
    const r = await fetch('/api/browse?path=' + encodeURIComponent(path));
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
  let html = '<div style="padding:1.5rem; max-width:720px;"><h3>Library folders</h3>';
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
  const list = document.getElementById('list');
  list.style.display = 'block';
  list.innerHTML = html;
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
  const parts = path ? path.split('/') : [];
  let html = '<a href="#" onclick="load(\\'\\'); return false;">Library</a>';
  let accum = '';
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

  list.innerHTML = html || '<p>Empty folder</p>';
}

loadFinished().then(function() { load(''); showContinue(); });
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
  }
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
      <div id="progress-info" style="margin-top:0.5rem; font-weight:500;"></div>
      <div style="margin-top:1rem;">
        <button onclick="document.getElementById('edit-meta').style.display='block'">Edit Info / Cover</button>
        <button onclick="addToCollection()" style="margin-left:0.5rem;">Add to Collection</button>
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
    document.getElementById('author').textContent = book.author || '';
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
        document.getElementById('progress-info').textContent = `Resume at ${Math.floor(saved.time/60)}m`;
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
          Track ${bm.fileIndex+1} @ ${Math.floor(bm.time/60)}:${(Math.floor(bm.time)%60).toString().padStart(2,'0')}
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

function renderTracks() {
  const ul = document.getElementById('tracks');
  ul.innerHTML = book.files.map((f, i) => {
    const safeName = (f.name || '').replace(/&/g, '&').replace(/</g, '<').replace(/"/g, '"');
    return `<li data-idx="${i}" style="display:flex; justify-content:space-between; align-items:center; gap:0.5rem;">
      <span style="flex:1;">${safeName}</span>
      <button type="button" onclick="playIndex(${i})">Play</button>
    </li>`;
  }).join('');
  updateActive();
}

function updateActive() {
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
    if (time > 1 && Math.abs(audio.currentTime - time) > 2) {
      try { audio.currentTime = time; } catch (e) {}
    }
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
  // Time info
  if (audio.duration) {
    const rem = audio.duration - audio.currentTime;
    document.getElementById('time-info').textContent = 
      `${Math.floor(audio.currentTime/60)}:${(Math.floor(audio.currentTime)%60).toString().padStart(2,'0')} / ${Math.floor(audio.duration/60)}:${(Math.floor(audio.duration)%60).toString().padStart(2,'0')} (${Math.floor(rem/60)}m left)`;
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

if __name__ == "__main__":
    print(f"Audiobook server on port {PORT}")
    print(f"Library: {AUDIO_DIR}")
    print(f"Music: {MUSIC_DIR}")
    print(f"Cover cache: {COVER_CACHE}")
    print(f"Users file: {USERS_FILE}")
    print(f"Library exists: {AUDIO_DIR.exists()}")
    print(f"Music exists: {MUSIC_DIR.exists()}")

    use_https = os.environ.get("HTTPS", "").lower() in ("1", "true", "yes")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    if use_https:
        cert = Path(__file__).parent / "server.crt"
        key = Path(__file__).parent / "server.key"
        if not cert.exists() or not key.exists():
            import subprocess
            print("Generating self-signed certificate...")
            subprocess.check_call([
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(key), "-out", str(cert),
                "-days", "3650", "-nodes",
                "-subj", "/CN=audiobook-server"
            ])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        print(f"HTTPS enabled (self-signed). Open https://YOUR_HOST:{PORT}/")
        print("Browser will warn about the certificate. For a real certificate,")
        print("leave HTTPS off and use Whatbox Managed Links instead.")
    else:
        print(f"HTTP on port {PORT}")
        if HTTPS_REDIRECT:
            print(f"Plain HTTP redirects to {HTTPS_REDIRECT}")
            print(f"Subsonic API: {HTTPS_REDIRECT}/rest")
        else:
            print("No HTTPS redirect yet. Create https-url.txt next to server.py")
            print("with one line, your Managed Link, for example:")
            print("  https://audiobooks.YOUR_SLOT.box.ca")
        print("For HTTPS: on Whatbox Manage Links, add a custom app named")
        print(f"  audiobooks  pointing at port {PORT}")
        print("Then open https://audiobooks.YOUR_SLOT.box.ca/")

    httpd.serve_forever()

