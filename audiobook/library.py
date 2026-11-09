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
from audiobook.config import AUDIO_DIR, AUDIO_EXTS, COVER_CACHE, COVER_NAMES, IMAGE_EXTS, LIBRARIES_FILE, MUSIC_DIR, MUSIC_FOLDER_ID, iso_now, log
from audiobook.store import load_extra_libraries, metadata
from audiobook.tags import audio_mime, extract_embedded_art, guess_duration, parse_track_filename, read_audio_meta

BOOKS_CACHE = {"data": None, "mtime": 0}

MUSIC_CACHE = {"data": None, "mtime": 0}

COVER_FETCH_LOCK = threading.Lock()

COVER_FETCHING = set()

def parse_name(name: str):
    if " - " in name:
        parts = name.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return None, name

def is_sidecar_meta(name: str) -> bool:
    lower = name.lower()
    return lower == "metadata.json" or lower.endswith(".metadata.json")

def strip_catalog_id(name: str) -> str:
    if not name:
        return name
    return re.sub(r"\s*\[[A-Za-z0-9]{8,}\]\s*$", "", str(name)).strip()

def people_names(value):
    names = []
    if isinstance(value, str) and value.strip():
        names.append(value.strip())
    elif isinstance(value, dict) and value.get("name"):
        names.append(str(value["name"]).strip())
    elif isinstance(value, list):
        for item in value:
            names.extend(people_names(item))
    return [name for name in names if name]

def read_sidecar(data):
    if not isinstance(data, dict):
        return None, None
    title = (data.get("title") or "").strip()
    subtitle = (data.get("subtitle") or "").strip()
    if title and subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}"
    authors = data.get("authors") if data.get("authors") is not None else data.get("author")
    return title or None, ", ".join(people_names(authors)) or None

def apply_sidecar(title, author, data):
    meta_title, meta_author = read_sidecar(data)
    if meta_title:
        title = meta_title
    if meta_author:
        author = meta_author
    return strip_catalog_id(title), strip_catalog_id(author) if author else author

def sidecar_genres(data):
    if not isinstance(data, dict):
        return "", []
    genres = []

    def take(ladder, genre_root_only):
        if not isinstance(ladder, dict):
            return
        root = str(ladder.get("root") or "")
        if genre_root_only and root and root.lower() not in ("genres", "genre"):
            return
        steps = ladder.get("ladder") or []
        if not steps or not isinstance(steps[0], dict):
            return
        name = str(steps[0].get("name") or "").strip()
        if name and name not in genres:
            genres.append(name)

    ladders = data.get("category_ladders") or []
    for ladder in ladders:
        take(ladder, True)
    if not genres:
        for ladder in ladders:
            take(ladder, False)
    return (genres[0] if genres else ""), genres

def _seconds_value(value):
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None

def runtime_seconds(data):
    if not isinstance(data, dict):
        return None
    if data.get("runtime_length_sec") is not None:
        return _seconds_value(data.get("runtime_length_sec"))
    if data.get("runtime_length_min") is not None:
        try:
            return max(0, int(round(float(data["runtime_length_min"]) * 60)))
        except (TypeError, ValueError):
            return None
    return None

def chapter_length_seconds(chapter):
    if not isinstance(chapter, dict):
        return None
    if chapter.get("length_ms") is not None:
        try:
            return max(0, int(float(chapter["length_ms"]) / 1000))
        except (TypeError, ValueError):
            return None
    if chapter.get("length_sec") is not None:
        return _seconds_value(chapter.get("length_sec"))
    return None

def fill_chapter_lengths(chapters, total=None):
    """Length is the next chapter's start, then a known runtime for the last one."""
    rows = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        row = {"title": chapter.get("title") or "", "start": max(0, int(chapter.get("start") or 0))}
        if chapter.get("length") is not None:
            row["length"] = max(0, int(chapter.get("length") or 0))
        rows.append(row)
    for i, row in enumerate(rows):
        if row.get("length") is None and i + 1 < len(rows):
            row["length"] = max(0, rows[i + 1]["start"] - row["start"])
    if rows and rows[-1].get("length") is None and total is not None:
        rows[-1]["length"] = max(0, int(total) - rows[-1]["start"])
    return rows

def sidecar_details(data):
    if not isinstance(data, dict):
        return "", [], "", "", []
    narrator = ", ".join(people_names(data.get("narrators") or data.get("narrator")))
    info = data.get("ChapterInfo") or {}
    raw = info.get("chapters") if isinstance(info, dict) else []
    chapters = []
    if isinstance(raw, list):
        for chapter in raw:
            if not isinstance(chapter, dict):
                continue
            title = (chapter.get("title") or "").strip()
            start = None
            if chapter.get("start_offset_sec") is not None:
                start = _seconds_value(chapter.get("start_offset_sec"))
            if start is None and chapter.get("start_offset_ms") is not None:
                try:
                    start = max(0, int(float(chapter["start_offset_ms"]) / 1000))
                except (TypeError, ValueError):
                    start = None
            if start is None or not title:
                continue
            row = {"title": title, "start": start}
            length = chapter_length_seconds(chapter)
            if length is not None:
                row["length"] = length
            chapters.append(row)
        chapters = fill_chapter_lengths(chapters, runtime_seconds(data))
    images = data.get("product_images") or {}
    cover_url = ""
    if isinstance(images, dict) and images:
        def rank(key):
            try:
                return int(str(key))
            except ValueError:
                return 0
        best = images.get(max(images, key=rank))
        if isinstance(best, str) and best.startswith("http"):
            cover_url = best
    genre, genres = sidecar_genres(data)
    return narrator, chapters, cover_url, genre, genres

def read_local_meta(rel):
    if not rel:
        return {}
    try:
        meta_file, _, _ = resolve_lib(rel)
        data = json.loads(meta_file.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"Local metadata read failed: {e}")
        return {}

def _series_position(value):
    if value is None or value == "":
        return None
    text = str(value).strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    raw = match.group(0)
    try:
        num = float(raw)
    except ValueError:
        return None
    if num.is_integer():
        return int(num)
    return num

def series_from_sidecar(data):
    if not isinstance(data, dict):
        return "", None
    series = data.get("series")
    if isinstance(series, str) and series.strip():
        pos = data.get("series_position")
        if pos is None:
            pos = data.get("seriesPosition")
        return series.strip(), _series_position(pos)
    candidates = []
    primary = data.get("seriesPrimary") or data.get("series_primary")
    if isinstance(primary, dict):
        candidates.append(primary)
    if isinstance(series, dict):
        candidates.append(series)
    if isinstance(series, list):
        candidates.extend(item for item in series if isinstance(item, dict))
    for item in candidates:
        name = (item.get("name") or item.get("title") or item.get("series") or "").strip()
        if not name:
            continue
        pos = item.get("position")
        if pos is None:
            pos = item.get("sequence")
        if pos is None:
            pos = item.get("index")
        if pos is None:
            pos = item.get("book_number")
        if pos is None:
            pos = item.get("bookNumber")
        return name, _series_position(pos)
    return "", None

def series_from_folder(rel, author):
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel or "/" not in rel:
        return "", None
    folder = Path(rel).name
    parent = Path(rel).parent.name
    wrapped = re.search(
        r"\((?P<name>[^()#]+?)\s*(?:#|,?\s*book)\s*(?P<num>\d+(?:\.\d+)?)\)\s*$",
        folder,
        re.I,
    )
    if wrapped:
        return wrapped.group("name").strip(), _series_position(wrapped.group("num"))
    numbered = re.match(
        r"^(?P<name>.+?)\s+#\s*(?P<num>\d+(?:\.\d+)?)\s*[-:]\s*.+$",
        folder,
    )
    if numbered:
        return numbered.group("name").strip(), _series_position(numbered.group("num"))
    leading = re.match(r"^(?:Book\s+)?(?P<num>\d+(?:\.\d+)?)[\s.\-_]+.+$", folder, re.I)
    if leading and parent and parent.lower() != (author or "").strip().lower():
        return parent, _series_position(leading.group("num"))
    return "", None

def describe_book(author, title, local_meta, file_count, folder_rel="", audio_rel=""):
    if author and re.fullmatch(r"(?:book\s+)?\d+(?:\.\d+)?", author.strip(), re.I):
        author = ""
    data = read_local_meta(local_meta)
    title, author = apply_sidecar(title, author, data)
    narrator, chapters, cover_url, genre, genres = sidecar_details(data)
    if file_count != 1:
        chapters = []
    series, series_index = series_from_sidecar(data)
    tags = {}
    if audio_rel and (not author or not series):
        path, _root, _idx = resolve_lib(audio_rel)
        if path and path.is_file():
            try:
                tags = read_audio_meta(path) or {}
            except Exception:
                tags = {}
    if not author:
        author = (tags.get("album_artist") or tags.get("artist") or "").strip()
    if not series:
        series = (tags.get("series") or "").strip()
        series_index = _series_position(tags.get("series_index"))
    if not series:
        series, series_index = series_from_folder(folder_rel, author)
    return title, author or "", narrator, chapters, cover_url, genre, genres, series or "", series_index

def save_remote_cover(url, book_id):
    if not url:
        return None
    dest = COVER_CACHE / f"{book_id}.jpg"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "audiobook-server"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(8000000)
        if len(data) < 1000:
            return None
        dest.write_bytes(data)
        return dest
    except Exception as e:
        log.warning(f"Cover download failed: {e}")
        return None

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
        log.warning(f"Open Library ISBN failed: {e}")

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
            log.warning(f"Google Books ISBN failed: {e}")

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
        log.warning(f"Google Books URL fetch failed: {e}")
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
        log.warning(f"Open Library fetch failed: {e}")
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
        log.warning(f"Open Library cover failed for {title}: {e}")

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
        log.warning(f"Google Books cover failed for {title}: {e}")

    return None

def get_book_id(rel_path: str) -> str:
    return base64.urlsafe_b64encode(rel_path.encode()).decode().rstrip("=")

def library_roots():
    roots = []
    seen = set()
    for raw in [str(AUDIO_DIR)] + load_extra_libraries():
        try:
            p = Path(raw).expanduser().resolve()
        except Exception:
            p = Path(raw).expanduser()
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
        items.append({
            "path": str(root),
            "name": root.name or str(root),
            "exists": root.is_dir(),
            "removable": i != 0,
        })
    return items

def scan_folder(rel: str = "", scope: str = "books"):
    """Return folders and books at a given relative path.

    scope "books" never shows the music library. scope "music" only shows it.
    """
    scope = "music" if scope == "music" else "books"
    if scope == "music":
        return scan_music_folder(rel)
    roots = library_roots()
    if not (rel or "").strip("/") and len(roots) > 1:
        folders = [{"name": (r.name or str(r)), "path": f"~{i}~"} for i, r in enumerate(roots)]
        return {"folders": folders, "books": [], "current": "", "rootName": "Libraries", "library": "books"}

    target, root, index = resolve_lib(rel)
    if target is None or not target.is_dir():
        return {"folders": [], "books": [], "current": rel, "rootName": "", "library": "books"}
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
            elif is_sidecar_meta(entry.name):
                local_meta = entry_rel
    except Exception as e:
        log.warning(f"Scan error: {e}")

    if files_in_this_dir:
        files_in_this_dir.sort(key=lambda f: f["name"])
        folder_name = Path(inner).name if inner else (root.name or "Library")
        author, title = parse_name(folder_name)
        folder_rel = join_lib_path(index, inner)
        title, author, narrator, chapters, cover_url, genre, genres, series, series_index = describe_book(
            author, title, local_meta, len(files_in_this_dir), folder_rel, files_in_this_dir[0]["rel"]
        )

        book_key = folder_rel or files_in_this_dir[0]["rel"]
        book_id = get_book_id(book_key)
        custom = metadata.get(book_id, {})
        books.append({
            "id": book_id,
            "name": custom.get("title") or title or folder_name,
            "author": custom.get("author") or author or "",
            "series": series,
            "seriesIndex": series_index,
            "narrator": narrator,
            "genre": genre,
            "genres": genres,
            "chapters": chapters,
            "coverUrl": cover_url,
            "files": files_in_this_dir,
            "cover": local_cover,
            "path": join_lib_path(index, inner),
            "kind": "audiobook",
        })

    return {"folders": folders, "books": books, "current": rel, "rootName": root.name or "", "library": "books"}

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
                elif is_sidecar_meta(entry.name):
                    local_meta = rel
            if files:
                files.sort(key=lambda f: f["name"])
                folder_name = Path(inner).name if inner else path.name
                author, title = parse_name(folder_name)
                if inner and "/" in inner:
                    parent = Path(inner).parent.name
                    if parent and not author:
                        author = parent
                folder_rel = join_lib_path(index, inner)
                title, author, narrator, chapters, cover_url, genre, genres, series, series_index = describe_book(
                    author, title, local_meta, len(files), folder_rel, files[0]["rel"]
                )
                book_key = (inner if index == 0 else folder_rel) or files[0]["rel"]
                book_id = get_book_id(book_key)
                custom = metadata.get(book_id, {})
                books.append({
                    "id": book_id,
                    "name": custom.get("title") or title or folder_name,
                    "author": custom.get("author") or author or "",
                    "series": series,
                    "seriesIndex": series_index,
                    "narrator": narrator,
                    "genre": genre,
                    "genres": genres,
                    "chapters": chapters,
                    "coverUrl": cover_url,
                    "files": files,
                    "cover": cover,
                    "path": join_lib_path(index, inner),
                    "totalSize": sum(f["size"] for f in files)
                })
        except Exception as e:
            log.warning(f"Walk error: {e}")

    for i, root in enumerate(library_roots()):
        walk(root, i, "")
    return books

def get_book_by_id(book_id: str):
    """Fast lookup for a single book by decoding its ID to a path."""
    if book_id.startswith("mal:") or book_id.startswith("ms:"):
        album = music_album_by_id(book_id)
        return music_as_book(album) if album else None
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
            elif is_sidecar_meta(entry.name):
                local_meta = entry_rel
    except Exception:
        pass

    if not files:
        return None

    files.sort(key=lambda f: f["name"])
    folder_name = Path(inner).name if inner else (root.name if root else "Library")
    author, title = parse_name(folder_name)
    folder_rel = join_lib_path(index, inner)
    title, author, narrator, chapters, cover_url, genre, genres, series, series_index = describe_book(
        author, title, local_meta, len(files), folder_rel, files[0]["rel"]
    )

    custom = metadata.get(book_id, {})
    return {
        "id": book_id,
        "name": custom.get("title") or title or folder_name,
        "author": custom.get("author") or author or "",
        "series": series,
        "seriesIndex": series_index,
        "narrator": narrator,
        "genre": genre,
        "genres": genres,
        "chapters": chapters,
        "coverUrl": cover_url,
        "files": files,
        "cover": cover,
        "path": join_lib_path(index, inner),
        "totalSize": sum(f["size"] for f in files)
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

def series_sort_key(book):
    idx = book.get("seriesIndex")
    if isinstance(idx, (int, float)) and not isinstance(idx, bool):
        num = float(idx)
    else:
        num = float("inf")
    return (num, (book.get("name") or "").lower())

def next_in_series(book):
    series = ((book or {}).get("series") or "").strip()
    if not series or not book.get("id"):
        return None
    same = [
        item for item in (get_books_cached() or [])
        if ((item.get("series") or "").strip().lower() == series.lower())
    ]
    same.sort(key=series_sort_key)
    ids = [item.get("id") for item in same]
    if book["id"] not in ids:
        return None
    rest = same[ids.index(book["id"]) + 1:]
    if not rest:
        return None
    item = rest[0]
    return {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "seriesIndex": item.get("seriesIndex"),
    }

def book_root_index(book):
    rel = book.get("path") or ""
    if not rel and book.get("files"):
        rel = book["files"][0].get("rel") or ""
    index, _inner = split_lib_path(rel)
    return index

def folder_books(folder_id=""):
    books = list(get_books_cached() or [])
    if folder_id.startswith("mf:"):
        rest = folder_id[3:]
        if rest == "music":
            return []
        try:
            want = int(rest)
        except ValueError:
            return books
        books = [b for b in books if book_root_index(b) == want]
    return books

def book_author(book):
    return book.get("author") or "Unknown"

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
    if book.get("coverUrl"):
        saved = save_remote_cover(book["coverUrl"], book_id)
        if saved:
            return saved
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

def media_id(prefix, text):
    return prefix + base64.urlsafe_b64encode((text or "").encode()).decode().rstrip("=")

def media_id_decode(sid, prefix):
    if not sid.startswith(prefix):
        return None
    raw = sid[len(prefix):]
    raw += "=" * ((4 - len(raw) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(raw).decode()
    except Exception:
        return None

def music_dir_ready():
    try:
        return MUSIC_DIR.is_dir()
    except Exception:
        return False

def music_folder_name():
    return MUSIC_DIR.name or "Music"

def media_kind(folder_id):
    fid = (folder_id or "").strip()
    if fid in (MUSIC_FOLDER_ID, "music") or fid == "mf:music":
        return "music"
    if fid.startswith("mf:") and fid[3:] == "music":
        return "music"
    if not fid:
        return "all"
    return "books"

def include_books(folder_id):
    return media_kind(folder_id) != "music"

def include_music(folder_id):
    return music_dir_ready() and media_kind(folder_id) != "books"

def resolve_music(rel):
    if not music_dir_ready():
        return None
    rel = (rel or "").replace("\\", "/").strip("/")
    if ".." in Path(rel).parts:
        return None
    try:
        root = MUSIC_DIR.resolve()
        target = (root / rel).resolve() if rel else root
    except Exception:
        return None
    if target != root and root not in target.parents:
        return None
    return target

def music_artist_key(name):
    return media_id("mar:", name or "Unknown")

def music_artist_name(aid):
    return media_id_decode(aid, "mar:")

def _dir_has_direct_audio(path):
    try:
        for entry in path.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTS:
                return True
    except Exception:
        pass
    return False

def _is_artist_folder(path):
    try:
        has_direct = False
        has_album = False
        for entry in path.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() in AUDIO_EXTS:
                has_direct = True
                break
            if entry.is_dir() and _dir_has_direct_audio(entry):
                has_album = True
        return (not has_direct) and has_album
    except Exception:
        return False

def _collect_dir_audio(path, rel):
    files = []
    cover = None
    try:
        entries = sorted(path.iterdir(), key=lambda e: e.name.lower())
    except Exception:
        return files, cover
    for entry in entries:
        if entry.name.startswith("."):
            continue
        child_rel = f"{rel}/{entry.name}" if rel else entry.name
        if entry.is_dir():
            nested, nested_cover = _collect_dir_audio(entry, child_rel)
            files.extend(nested)
            if nested_cover and cover is None:
                cover = nested_cover
        elif entry.suffix.lower() in AUDIO_EXTS:
            try:
                files.append({"name": entry.name, "rel": child_rel, "size": entry.stat().st_size, "path": entry})
            except Exception:
                continue
        elif entry.suffix.lower() in IMAGE_EXTS:
            if entry.name.lower() in COVER_NAMES:
                cover = child_rel
            elif cover is None:
                cover = child_rel
    files.sort(key=lambda f: f["name"].lower())
    return files, cover

def _music_file_info(entry, album_name, album_artist):
    path = entry["path"]
    meta = read_audio_meta(path)
    fn_track, fn_title = parse_track_filename(Path(entry["name"]).stem)
    title = meta.get("title") or fn_title
    artist = meta.get("artist") or album_artist
    album = meta.get("album") or album_name
    track = meta.get("track") or fn_track or 0
    duration = int(meta.get("duration") or 0)
    size = int(entry.get("size") or 0)
    bitrate = 128
    if duration > 0 and size > 0:
        bitrate = max(32, int(size * 8 / duration / 1000))
    return {
        "id": media_id("ms:", entry["rel"]),
        "name": entry["name"],
        "rel": entry["rel"],
        "size": size,
        "title": title,
        "artist": artist,
        "album": album,
        "track": int(track) if track else 0,
        "duration": duration,
        "bitRate": bitrate,
        "contentType": audio_mime(entry["name"]),
        "suffix": Path(entry["name"]).suffix.lower().lstrip(".") or "mp3",
    }

def _add_music_album(albums, path, rel, artist, album_name, files, cover):
    if not files:
        return
    created = iso_now()
    try:
        created = iso_now(path.stat().st_mtime)
    except Exception:
        pass
    infos = [_music_file_info(f, album_name, artist) for f in files]
    for i, info in enumerate(infos):
        if not info.get("track"):
            info["track"] = i + 1
    albums.append({
        "id": media_id("mal:", rel or album_name),
        "path": rel,
        "name": album_name,
        "artist": artist or "Unknown",
        "artistId": music_artist_key(artist or "Unknown"),
        "cover": cover,
        "created": created,
        "files": infos,
    })

def scan_music_full():
    albums = []
    if not music_dir_ready():
        return albums

    def walk(path, rel, artist=None):
        try:
            entries = sorted(path.iterdir(), key=lambda e: e.name.lower())
        except Exception as e:
            log.warning(f"Music walk error: {e}")
            return
        files = []
        cover = None
        dirs = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_dir():
                dirs.append((entry, child_rel))
            elif entry.suffix.lower() in AUDIO_EXTS:
                try:
                    files.append({"name": entry.name, "rel": child_rel, "size": entry.stat().st_size, "path": entry})
                except Exception:
                    continue
            elif entry.suffix.lower() in IMAGE_EXTS:
                if entry.name.lower() in COVER_NAMES:
                    cover = child_rel
                elif cover is None:
                    cover = child_rel
        if files:
            folder_name = Path(rel).name if rel else "Music"
            album_artist = artist
            album_name = folder_name
            if not rel:
                album_artist = album_artist or "Unknown"
                album_name = "Music"
            elif not album_artist:
                parsed_artist, parsed_title = parse_name(folder_name)
                if parsed_artist:
                    album_artist = parsed_artist
                    album_name = parsed_title or folder_name
                else:
                    album_artist = "Unknown"
            files.sort(key=lambda f: f["name"].lower())
            _add_music_album(albums, path, rel or "_root_", album_artist, album_name, files, cover)
        treat_as_artist = (path != MUSIC_DIR and artist is None and _is_artist_folder(path))
        next_artist = path.name if treat_as_artist else artist
        for d, child_rel in dirs:
            if treat_as_artist:
                nested_files, nested_cover = _collect_dir_audio(d, child_rel)
                if nested_files:
                    _add_music_album(albums, d, child_rel, next_artist, d.name, nested_files, nested_cover)
                else:
                    walk(d, child_rel, next_artist)
            else:
                walk(d, child_rel, next_artist)

    walk(MUSIC_DIR, "", None)
    return albums

def get_music_cached(force=False):
    if not music_dir_ready():
        MUSIC_CACHE["data"] = []
        MUSIC_CACHE["mtime"] = 0
        return []
    try:
        current_mtime = MUSIC_DIR.stat().st_mtime
    except Exception:
        current_mtime = 0
    if force or MUSIC_CACHE["data"] is None or current_mtime != MUSIC_CACHE.get("mtime", 0):
        MUSIC_CACHE["data"] = scan_music_full()
        MUSIC_CACHE["mtime"] = current_mtime
        MUSIC_CACHE["ts"] = time.time()
    return MUSIC_CACHE["data"] or []

def music_albums(folder_id=""):
    if not include_music(folder_id):
        return []
    return get_music_cached()

def music_album_by_id(sid):
    if sid.startswith("ms:"):
        album, _info = music_song_by_id(sid)
        return album
    if not sid.startswith("mal:"):
        return None
    for album in get_music_cached():
        if album["id"] == sid:
            return album
    rel = media_id_decode(sid, "mal:")
    if rel:
        for album in get_music_cached():
            if album.get("path") == rel:
                return album
    return None

def music_song_by_id(sid):
    rel = media_id_decode(sid, "ms:")
    if rel is None:
        return None, None
    for album in get_music_cached():
        for info in album.get("files") or []:
            if info.get("id") == sid or info.get("rel") == rel:
                return album, info
    return None, None

def music_song_obj(album, info):
    ext = info.get("suffix") or Path(info.get("name") or "").suffix.lower().lstrip(".")
    return {
        "id": info["id"],
        "parent": album["id"],
        "isDir": False,
        "title": info.get("title") or Path(info.get("name") or "Track").stem,
        "album": info.get("album") or album.get("name") or "Untitled",
        "artist": info.get("artist") or album.get("artist") or "Unknown",
        "track": int(info.get("track") or 0),
        "coverArt": album["id"],
        "size": int(info.get("size") or 0),
        "contentType": info.get("contentType") or audio_mime(info.get("name") or ""),
        "suffix": ext or "mp3",
        "duration": int(info.get("duration") or 0),
        "bitRate": int(info.get("bitRate") or 128),
        "path": info.get("rel") or info.get("name") or "",
        "albumId": album["id"],
        "artistId": album.get("artistId") or music_artist_key(album.get("artist")),
        "type": "music",
    }

def music_album_obj(album, with_songs=False):
    songs = [music_song_obj(album, f) for f in album.get("files") or []]
    obj = {
        "id": album["id"],
        "name": album.get("name") or "Untitled",
        "title": album.get("name") or "Untitled",
        "album": album.get("name") or "Untitled",
        "artist": album.get("artist") or "Unknown",
        "artistId": album.get("artistId") or music_artist_key(album.get("artist")),
        "coverArt": album["id"],
        "songCount": len(songs),
        "duration": sum(int(s.get("duration") or 0) for s in songs),
        "created": album.get("created") or iso_now(),
    }
    if with_songs:
        obj["song"] = songs
    return obj

def music_as_book(album):
    files = [{
        "name": f.get("name"),
        "rel": f.get("rel"),
        "size": int(f.get("size") or 0),
        "duration": int(f.get("duration") or 0),
    } for f in album.get("files") or []]
    return {
        "id": album["id"],
        "name": album.get("name") or "Untitled",
        "author": album.get("artist") or "",
        "files": files,
        "cover": album.get("cover"),
        "path": album.get("path") or "",
        "kind": "music",
        "totalSize": sum(f["size"] for f in files),
    }

def scan_music_folder(rel: str = ""):
    rel = (rel or "").replace("\\", "/").strip("/")
    if ".." in Path(rel).parts:
        rel = ""
    if not music_dir_ready():
        return {"folders": [], "books": [], "current": rel, "rootName": "Music", "library": "music", "missing": True}
    target = resolve_music(rel)
    if target is None or not target.is_dir():
        return {"folders": [], "books": [], "current": rel, "rootName": "Music", "library": "music"}
    folders = []
    books = []
    files_in_this_dir = []
    local_cover = None
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_dir():
                folders.append({"name": entry.name, "path": child_rel})
            elif entry.suffix.lower() in AUDIO_EXTS:
                try:
                    files_in_this_dir.append({"name": entry.name, "rel": child_rel, "size": entry.stat().st_size})
                except Exception:
                    continue
            elif entry.suffix.lower() in IMAGE_EXTS:
                if entry.name.lower() in COVER_NAMES:
                    local_cover = child_rel
                elif local_cover is None:
                    local_cover = child_rel
    except Exception as e:
        log.warning(f"Music scan error: {e}")
    if files_in_this_dir:
        want = media_id("mal:", rel or "_root_")
        album = next((a for a in get_music_cached() if a.get("id") == want or a.get("path") == (rel or "_root_")), None)
        if album:
            books.append(music_as_book(album))
        else:
            folder_name = Path(rel).name if rel else "Music"
            parsed_artist, parsed_title = parse_name(folder_name)
            books.append({
                "id": want,
                "name": parsed_title or folder_name,
                "author": parsed_artist or "",
                "files": sorted(files_in_this_dir, key=lambda f: f["name"].lower()),
                "cover": local_cover,
                "path": rel,
                "kind": "music",
            })
    return {"folders": folders, "books": books, "current": rel, "rootName": "Music", "library": "music"}

def resolve_item_file(book, rel):
    if (book or {}).get("kind") == "music" or str((book or {}).get("id") or "").startswith("mal:"):
        return resolve_music(rel)
    path, _root, _idx = resolve_lib(rel or "")
    return path

def music_artists_from(albums=None):
    grouped = {}
    for album in albums if albums is not None else get_music_cached():
        grouped.setdefault(album.get("artist") or "Unknown", []).append(album)
    return grouped

def selected_books(folder_id):
    if not include_books(folder_id):
        return []
    return folder_books("" if media_kind(folder_id) == "all" else folder_id)

def music_cover_file(album, song_info=None):
    if album and album.get("cover"):
        path = resolve_music(album["cover"])
        if path and path.is_file():
            return path
    files = []
    if song_info:
        files.append(song_info)
    if album:
        files.extend(album.get("files") or [])
    seen = set()
    for info in files:
        rel = info.get("rel")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        path = resolve_music(rel)
        if path and path.is_file():
            pic = extract_embedded_art(path)
            if pic:
                cache = COVER_CACHE / ("m-" + hashlib.sha1(rel.encode()).hexdigest()[:16] + ".img")
                try:
                    if not cache.exists() or cache.stat().st_size != len(pic[0]):
                        cache.write_bytes(pic[0])
                    return cache
                except Exception:
                    return None
    return None

def attach_file_duration(book):
    """Fill a missing last-chapter length from a real audio header, not a bitrate guess."""
    if not book:
        return book
    chapters = book.get("chapters") or []
    if not chapters or chapters[-1].get("length") is not None:
        return book
    files = book.get("files") or []
    if len(files) != 1:
        return book
    path = resolve_item_file(book, files[0].get("rel") or "")
    if path is None or not path.is_file():
        return book
    try:
        size = path.stat().st_size
    except Exception:
        return book
    duration = int(read_audio_meta(path).get("duration") or 0)
    if not duration or duration == guess_duration(size):
        return book
    if duration <= int(chapters[-1].get("start") or 0):
        return book
    updated = dict(book)
    updated["chapters"] = fill_chapter_lengths(chapters, duration)
    return updated
