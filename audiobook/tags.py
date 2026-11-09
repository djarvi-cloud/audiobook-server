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

def guess_duration(size):
    if not size:
        return 0
    return max(1, int(int(size) * 8 / 128000))

def audio_mime(name):
    ext = Path(name).suffix.lower()
    if ext in (".m4b", ".m4a", ".mp4"):
        return "audio/mp4"
    if ext == ".mp3":
        return "audio/mpeg"
    if ext == ".flac":
        return "audio/flac"
    if ext in (".ogg", ".opus"):
        return "audio/ogg"
    if ext == ".aac":
        return "audio/aac"
    if ext == ".wav":
        return "audio/wav"
    return mimetypes.guess_type(str(name))[0] or "application/octet-stream"

TRACK_NAME_RE = re.compile(r"^(\d{1,3})[\s.\-_]+(.+)$")

def parse_track_filename(stem):
    name = stem or ""
    m = TRACK_NAME_RE.match(name)
    if m:
        try:
            return int(m.group(1)), m.group(2).strip() or name
        except ValueError:
            return None, name
    return None, name

def _id3_synchsafe(b):
    return ((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14) | ((b[2] & 0x7F) << 7) | (b[3] & 0x7F)

def _decode_id3_txxx(data):
    if not data:
        return "", ""
    enc = data[0]
    payload = data[1:]
    try:
        if enc in (1, 2):
            text = payload.decode("utf-16" if enc == 1 else "utf-16-be", "replace")
            parts = text.split("\x00")
            return (parts[0] if parts else "").strip(), (parts[1] if len(parts) > 1 else "").strip()
        sep = payload.find(b"\x00")
        if sep < 0:
            return payload.decode("utf-8" if enc == 3 else "latin-1", "replace").strip(), ""
        desc = payload[:sep].decode("utf-8" if enc == 3 else "latin-1", "replace")
        val = payload[sep + 1:].split(b"\x00", 1)[0].decode("utf-8" if enc == 3 else "latin-1", "replace")
        return desc.strip(), val.strip()
    except Exception:
        return "", ""

def _take_series_tag(out, desc, val):
    if not val:
        return
    key = (desc or "").strip().lower().replace("_", " ").replace("-", " ")
    if key in ("series", "series name", "mvnm", "movement name", "grouping", "content group"):
        out.setdefault("series", val.strip())
    elif key in ("series part", "series index", "series number", "part", "mvin", "movement", "movement index"):
        out.setdefault("series_index", val.strip())

def _decode_id3_text(data):
    if not data:
        return ""
    enc = data[0]
    payload = data[1:]
    try:
        if enc == 0:
            return payload.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()
        if enc == 1:
            text = payload.decode("utf-16", "replace")
            return text.split("\x00", 1)[0].strip()
        if enc == 2:
            return payload.decode("utf-16-be", "replace").split("\x00", 1)[0].strip()
        if enc == 3:
            return payload.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    except Exception:
        pass
    return payload.decode("latin-1", "replace").strip()

def _parse_apic(data):
    if not data:
        return None
    rest = data[1:]
    z = rest.find(b"\x00")
    if z < 0:
        return None
    mime = rest[:z].decode("latin-1", "replace") or "image/jpeg"
    rest = rest[z + 1:]
    if not rest:
        return None
    rest = rest[1:]
    enc = data[0]
    if enc in (1, 2):
        z = 0
        while z + 1 < len(rest):
            if rest[z:z + 2] == b"\x00\x00":
                break
            z += 2
        img = rest[z + 2:] if z + 1 < len(rest) else b""
    else:
        z = rest.find(b"\x00")
        img = rest[z + 1:] if z >= 0 else rest
    if len(img) < 24:
        return None
    if mime in ("image/jpg", "image/jpeg", "image/png", "image/webp", "image/gif"):
        return img, mime
    if img[:3] == b"\xff\xd8\xff":
        return img, "image/jpeg"
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return img, "image/png"
    return img, mime or "image/jpeg"

def _parse_vorbis_comment(data):
    out = {}
    if len(data) < 8:
        return out
    vend_len = struct.unpack("<I", data[0:4])[0]
    off = 4 + vend_len
    if off + 4 > len(data):
        return out
    n = struct.unpack("<I", data[off:off + 4])[0]
    off += 4
    mapping = {
        "title": "title", "artist": "artist", "album": "album",
        "albumartist": "album_artist", "album artist": "album_artist",
        "series": "series", "series-part": "series_index", "seriespart": "series_index",
        "movementname": "series", "movement": "series_index",
        "tracknumber": "track", "track": "track", "tracktotal": None,
    }
    for _ in range(max(0, min(n, 256))):
        if off + 4 > len(data):
            break
        ln = struct.unpack("<I", data[off:off + 4])[0]
        off += 4
        if ln < 0 or off + ln > len(data):
            break
        rec = data[off:off + ln].decode("utf-8", "replace")
        off += ln
        if "=" not in rec:
            continue
        k, v = rec.split("=", 1)
        key = mapping.get(k.strip().lower())
        if not key or not v:
            continue
        if key == "track":
            try:
                out["track"] = int(v.split("/")[0].strip())
            except ValueError:
                pass
        else:
            out.setdefault(key, v.strip())
    return out

def _read_id3(path, want_picture=False):
    out = {}
    try:
        with open(path, "rb") as f:
            hdr = f.read(10)
            if len(hdr) >= 10 and hdr[:3] == b"ID3":
                ver = hdr[3]
                size = _id3_synchsafe(hdr[6:10])
                tag = f.read(min(size, 2 * 1024 * 1024))
                off = 0
                if hdr[5] & 0x40 and len(tag) >= 4:
                    ext = _id3_synchsafe(tag[0:4]) if ver >= 4 else struct.unpack(">I", tag[0:4])[0]
                    off = min(max(ext, 4), len(tag))
                while off + 10 <= len(tag):
                    fid = tag[off:off + 4]
                    if fid == b"\x00\x00\x00\x00" or not fid.replace(b"\x00", b""):
                        break
                    if not all(65 <= c <= 90 or 48 <= c <= 57 for c in fid):
                        break
                    if ver >= 4:
                        flen = _id3_synchsafe(tag[off + 4:off + 8])
                    else:
                        flen = struct.unpack(">I", tag[off + 4:off + 8])[0]
                    if flen < 0 or off + 10 + flen > len(tag):
                        break
                    data = tag[off + 10:off + 10 + flen]
                    off += 10 + flen
                    if fid == b"TIT2":
                        out["title"] = _decode_id3_text(data)
                    elif fid == b"TPE1":
                        out["artist"] = _decode_id3_text(data)
                    elif fid == b"TPE2":
                        out["album_artist"] = _decode_id3_text(data)
                    elif fid == b"TXXX":
                        desc, val = _decode_id3_txxx(data)
                        _take_series_tag(out, desc, val)
                    elif fid == b"TALB":
                        out["album"] = _decode_id3_text(data)
                    elif fid == b"TRCK":
                        raw = _decode_id3_text(data).split("/")[0].strip()
                        try:
                            out["track"] = int(raw)
                        except ValueError:
                            pass
                    elif fid == b"TLEN":
                        raw = _decode_id3_text(data).strip()
                        try:
                            ms = int(raw)
                            if ms > 0:
                                out["duration"] = max(1, ms // 1000)
                        except ValueError:
                            pass
                    elif want_picture and fid == b"APIC" and "picture" not in out:
                        pic = _parse_apic(data)
                        if pic:
                            out["picture"] = pic
                out["_id3_end"] = 10 + size
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size >= 128:
                f.seek(file_size - 128)
                v1 = f.read(128)
                if v1[:3] == b"TAG":
                    def t(b):
                        return b.decode("latin-1", "replace").split("\x00", 1)[0].strip()
                    out.setdefault("title", t(v1[3:33]))
                    out.setdefault("artist", t(v1[33:63]))
                    out.setdefault("album", t(v1[63:93]))
                    if v1[125] == 0 and v1[126] != 0:
                        out.setdefault("track", v1[126])
    except Exception:
        pass
    return out

def _mp3_xing_duration(path, start):
    try:
        with open(path, "rb") as f:
            f.seek(start or 0)
            buf = f.read(8192)
        sync = buf.find(b"\xff")
        while sync >= 0 and sync + 4 <= len(buf):
            b1, b2 = buf[sync], buf[sync + 1]
            if b1 == 0xFF and (b2 & 0xE0) == 0xE0:
                break
            sync = buf.find(b"\xff", sync + 1)
        else:
            return 0
        hdr = buf[sync:sync + 4]
        ver_id = (hdr[1] >> 3) & 0x03
        layer = (hdr[1] >> 1) & 0x03
        sr_idx = (hdr[2] >> 2) & 0x03
        ch_mode = (hdr[3] >> 6) & 0x03
        sr_table = {
            3: [44100, 48000, 32000],
            2: [22050, 24000, 16000],
            0: [11025, 12000, 8000],
        }
        rates = sr_table.get(ver_id)
        if not rates or sr_idx > 2:
            return 0
        sample_rate = rates[sr_idx]
        if layer == 1:
            spf = 384
        elif layer == 2:
            spf = 1152
        else:
            spf = 1152 if ver_id == 3 else 576
        if ver_id == 3:
            side = 32 if ch_mode != 3 else 17
        else:
            side = 17 if ch_mode != 3 else 9
        xoff = sync + 4 + side
        if xoff + 16 > len(buf):
            return 0
        tag = buf[xoff:xoff + 4]
        if tag not in (b"Xing", b"Info"):
            return 0
        flags = struct.unpack(">I", buf[xoff + 4:xoff + 8])[0]
        if not (flags & 1):
            return 0
        frames = struct.unpack(">I", buf[xoff + 8:xoff + 12])[0]
        if frames and sample_rate:
            return max(1, int(frames * spf / sample_rate))
    except Exception:
        return 0
    return 0

def _read_flac_meta(path, want_picture=False):
    out = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return out
            last = False
            while not last:
                hdr = f.read(4)
                if len(hdr) < 4:
                    break
                last = bool(hdr[0] & 0x80)
                btype = hdr[0] & 0x7F
                ln = int.from_bytes(hdr[1:4], "big")
                if ln > 8 * 1024 * 1024:
                    break
                data = f.read(ln)
                if len(data) < ln:
                    break
                if btype == 0 and len(data) >= 18:
                    info = int.from_bytes(data[10:18], "big")
                    sample_rate = (info >> 44) & 0xFFFFF
                    total = info & 0xFFFFFFFFF
                    if sample_rate:
                        out["duration"] = max(1, int(total / sample_rate)) if total else 0
                elif btype == 4:
                    out.update(_parse_vorbis_comment(data))
                elif want_picture and btype == 6 and "picture" not in out and len(data) >= 32:
                    off = 4
                    mime_len = struct.unpack(">I", data[off:off + 4])[0]
                    off += 4
                    mime = data[off:off + mime_len].decode("latin-1", "replace") if off + mime_len <= len(data) else "image/jpeg"
                    off += mime_len
                    if off + 4 > len(data):
                        continue
                    desc_len = struct.unpack(">I", data[off:off + 4])[0]
                    off += 4 + desc_len + 16
                    if off + 4 > len(data):
                        continue
                    pic_len = struct.unpack(">I", data[off:off + 4])[0]
                    off += 4
                    pic = data[off:off + pic_len]
                    if len(pic) >= 24:
                        out["picture"] = (pic, mime or "image/jpeg")
    except Exception:
        pass
    return out

def _iter_mp4_boxes(data, start, end):
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", data[off:off + 4])[0]
        typ = data[off + 4:off + 8]
        hdr = 8
        if size == 1 and off + 16 <= end:
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            break
        yield typ, off + hdr, off + size
        off += size

def _mp4_data_payload(buf, start, end):
    for typ, s, e in _iter_mp4_boxes(buf, start, end):
        if typ == b"data" and e - s >= 8:
            return buf[s + 8:e]
    return b""

def _read_mp4_freeform(blob, start, end, out):
    mean = name = val = ""
    for typ, s, e in _iter_mp4_boxes(blob, start, end):
        raw = blob[s:e]
        if typ == b"mean" and len(raw) > 4:
            mean = raw[4:].decode("utf-8", "replace").strip()
        elif typ == b"name" and len(raw) > 4:
            name = raw[4:].decode("utf-8", "replace").strip()
        elif typ == b"data" and len(raw) > 8:
            val = raw[8:].decode("utf-8", "replace").strip()
    if not val:
        return
    if mean and "itunes" not in mean.lower() and "apple" not in mean.lower():
        return
    _take_series_tag(out, name, val)

def _read_mp4_meta(path, want_picture=False):
    out = {}
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(min(size, 512 * 1024))
            blob = head
            if b"moov" not in head and size > 512 * 1024:
                f.seek(max(0, size - 512 * 1024))
                blob = f.read()
        end = len(blob)
        def walk(start, stop, depth=0):
            if depth > 12:
                return
            for typ, s, e in _iter_mp4_boxes(blob, start, stop):
                if typ in (b"moov", b"udta", b"meta", b"ilst", b"trak", b"mdia"):
                    ns = s + (4 if typ == b"meta" else 0)
                    walk(ns, e, depth + 1)
                elif typ == b"mvhd" and "duration" not in out and e - s >= 20:
                    version = blob[s]
                    if version == 1 and e - s >= 32:
                        timescale = struct.unpack(">I", blob[s + 20:s + 24])[0]
                        duration = struct.unpack(">Q", blob[s + 24:s + 32])[0]
                    else:
                        timescale = struct.unpack(">I", blob[s + 12:s + 16])[0]
                        duration = struct.unpack(">I", blob[s + 16:s + 20])[0]
                    if timescale:
                        out["duration"] = max(1, int(duration / timescale)) if duration else 0
                elif typ == b"\xa9nam":
                    val = _mp4_data_payload(blob, s, e).decode("utf-8", "replace").strip()
                    if val:
                        out["title"] = val
                elif typ == b"\xa9ART":
                    val = _mp4_data_payload(blob, s, e).decode("utf-8", "replace").strip()
                    if val:
                        out.setdefault("artist", val)
                elif typ == b"aART":
                    val = _mp4_data_payload(blob, s, e).decode("utf-8", "replace").strip()
                    if val:
                        out["album_artist"] = val
                        out.setdefault("artist", val)
                elif typ == b"\xa9grp":
                    val = _mp4_data_payload(blob, s, e).decode("utf-8", "replace").strip()
                    if val:
                        out.setdefault("series", val)
                elif typ == b"----":
                    _read_mp4_freeform(blob, s, e, out)
                elif typ == b"\xa9alb":
                    val = _mp4_data_payload(blob, s, e).decode("utf-8", "replace").strip()
                    if val:
                        out["album"] = val
                elif typ == b"trkn":
                    payload = _mp4_data_payload(blob, s, e)
                    if len(payload) >= 4:
                        out["track"] = struct.unpack(">H", payload[2:4])[0]
                elif want_picture and typ == b"covr" and "picture" not in out:
                    payload = _mp4_data_payload(blob, s, e)
                    if len(payload) >= 24:
                        mime = "image/png" if payload[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                        out["picture"] = (payload, mime)
        walk(0, end)
    except Exception:
        pass
    return out

def _ogg_pages(data):
    off = 0
    while True:
        i = data.find(b"OggS", off)
        if i < 0 or i + 27 > len(data):
            break
        nseg = data[i + 26]
        if i + 27 + nseg > len(data):
            break
        segs = data[i + 27:i + 27 + nseg]
        body_len = sum(segs)
        body_start = i + 27 + nseg
        body_end = body_start + body_len
        if body_end > len(data):
            break
        granule = struct.unpack("<Q", data[i + 6:i + 14])[0]
        yield granule, data[body_start:body_end]
        off = body_end

def _read_ogg_meta(path):
    out = {}
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(min(size, 128 * 1024))
            tail = b""
            if size > 128 * 1024:
                f.seek(max(0, size - 64 * 1024))
                tail = f.read()
        sample_rate = 0
        last_granule = 0
        is_opus = False
        packets = []
        for granule, body in _ogg_pages(head):
            packets.append(body)
            last_granule = granule or last_granule
        for granule, _body in _ogg_pages(tail):
            last_granule = granule or last_granule
        for pkt in packets[:4]:
            if pkt.startswith(b"OpusHead") and len(pkt) >= 16:
                is_opus = True
                sample_rate = 48000
            elif pkt.startswith(b"OpusTags"):
                out.update(_parse_vorbis_comment(pkt[8:]))
            elif len(pkt) > 7 and pkt[0] == 1 and pkt[1:7] == b"vorbis":
                if len(pkt) >= 16:
                    sample_rate = struct.unpack("<I", pkt[12:16])[0]
            elif len(pkt) > 7 and pkt[0] == 3 and pkt[1:7] == b"vorbis":
                out.update(_parse_vorbis_comment(pkt[7:]))
        if last_granule and (sample_rate or is_opus):
            rate = 48000 if is_opus else sample_rate
            if rate:
                out["duration"] = max(1, int(last_granule / rate))
    except Exception:
        pass
    return out

def _read_wav_meta(path):
    out = {}
    try:
        with open(path, "rb") as f:
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[0:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                return out
            byte_rate = 0
            data_size = 0
            while True:
                chunk = f.read(8)
                if len(chunk) < 8:
                    break
                cid, clen = chunk[0:4], struct.unpack("<I", chunk[4:8])[0]
                if cid == b"fmt " and clen >= 16:
                    fmt = f.read(clen)
                    if len(fmt) >= 16:
                        byte_rate = struct.unpack("<I", fmt[8:12])[0]
                    continue
                if cid == b"data":
                    data_size = clen
                    break
                f.seek(clen + (clen & 1), os.SEEK_CUR)
            if byte_rate:
                out["duration"] = max(1, int(data_size / byte_rate)) if data_size else 0
    except Exception:
        pass
    return out

def read_audio_meta(path, want_picture=False):
    result = {"title": None, "artist": None, "album": None, "track": None, "duration": 0}
    if want_picture:
        result["picture"] = None
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            got = _read_id3(path, want_picture=want_picture)
            result.update({k: v for k, v in got.items() if not k.startswith("_") and v})
            if not result.get("duration"):
                result["duration"] = _mp3_xing_duration(path, got.get("_id3_end") or 0)
        elif ext == ".flac":
            result.update({k: v for k, v in _read_flac_meta(path, want_picture=want_picture).items() if v})
        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            result.update({k: v for k, v in _read_mp4_meta(path, want_picture=want_picture).items() if v})
        elif ext in (".ogg", ".opus"):
            result.update({k: v for k, v in _read_ogg_meta(path).items() if v})
        elif ext == ".wav":
            result.update({k: v for k, v in _read_wav_meta(path).items() if v})
    except Exception:
        pass
    if not result.get("duration"):
        try:
            result["duration"] = guess_duration(path.stat().st_size)
        except Exception:
            result["duration"] = 0
    return result

def extract_embedded_art(path):
    try:
        meta = read_audio_meta(path, want_picture=True)
        pic = meta.get("picture")
        if pic and pic[0]:
            return pic
    except Exception:
        pass
    return None
