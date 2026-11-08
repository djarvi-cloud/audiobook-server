# Audiobook server

Single-file Python audiobook (and music) server for a Whatbox seedbox. No extra packages. OpenSubsonic / Subsonic compatible.

## Install

```bash
git clone https://github.com/djarvi-cloud/audiobook-server.git
cd audiobook-server
./install.sh
```

Books default to `~/files/audiobooks`. Music defaults to `~/files/music`. Override with `AUDIO_DIR`, `MUSIC_DIR`, and `PORT` if needed:

```bash
AUDIO_DIR=~/files/audiobooks MUSIC_DIR=~/files/music PORT=17930 ./install.sh
```

Log in at https://audobooks.covertsa.com once after the first start so Subsonic apps can connect. Default user is `admin` / `changeme` until you change it.

## Music and playlists

- Put music under `MUSIC_DIR` (artist/album folders or simple album folders). It appears as an additional music folder in Subsonic clients.
- Playlists are stored in `playlists.json` (not in git). Create, update, and delete them from any Subsonic client. A playlist is visible to its owner; others see it only if marked public.

## Update

```bash
cd ~/audiobook-server
git pull
./install.sh
```

`users.json`, progress, covers, playlists, and other data files are not in git.
