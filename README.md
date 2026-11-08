# Audiobook server

Single-file Python audiobook (and music) server for a Whatbox seedbox. No extra packages. OpenSubsonic / Subsonic compatible.

## Install

```bash
git clone https://github.com/djarvi-cloud/audiobook-server.git
cd audiobook-server
./install.sh
```

Books default to `~/files/audiobooks`. Music defaults to `~/files/music`. If the music folder is missing, it is skipped. Override with `AUDIO_DIR`, `MUSIC_DIR`, and `PORT` if needed:

```bash
AUDIO_DIR=~/files/audiobooks MUSIC_DIR=~/files/music PORT=17930 ./install.sh
```

Music is scanned the way Subsonic clients expect: `Artist/Album/Song` when that layout is present, otherwise each folder with audio files is an album. The website has separate Audiobooks and Music tabs. Subsonic clients see music as its own folder (`getMusicFolders`). Audiobook progress, collections, and finished-book lists stay book-only.

Playlists are stored in `playlists.json` (not in git). Each logged-in user creates and manages their own from the website (Playlists) or a Subsonic client. A playlist is visible to other users only when it is public, and only the owner can change or delete it. A playlist may include music tracks and audiobook chapters.

Log in at https://audobooks.covertsa.com once after the first start so Subsonic apps can connect. Default user is `admin` / `changeme` until you change it.

## Update

```bash
cd ~/audiobook-server
git pull
./install.sh
```

`users.json`, progress, playlists, covers, and other data files are not in git.

## Logs

Requests, logins, Subsonic calls, and errors are written to `audiobook.log` next to `server.py` and to the screen session. The file rotates at 5 MB and keeps 3 old copies. Passwords and Subsonic tokens are not logged.

```bash
tail -f ~/audiobook-server/audiobook.log
```

Cover art, progress saves, pings, and byte-range streams are debug-only. Turn them on with `LOG_LEVEL`:

```bash
LOG_LEVEL=DEBUG ./install.sh
```

`LOG_FILE`, `LOG_MAX_BYTES`, and `LOG_BACKUPS` override the log path and rotation.
