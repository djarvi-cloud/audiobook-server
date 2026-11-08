# Audiobook server

Single-file Python audiobook server for a Whatbox seedbox. No extra packages.

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

Music is scanned the way Subsonic clients expect: `Artist/Album/Song` when that layout is present, otherwise each folder with audio files is an album. It shows up as its own music folder in Subsonic (`getMusicFolders`). Audiobook progress, collections, and the book web UI stay book-only.

Playlists are stored in `playlists.json` next to the other data files and are managed through the Subsonic API (`getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`, `deletePlaylist`). The owner is the authenticated user. Other users can see a playlist only when it is public; only the owner can change or delete it. A playlist may include music tracks and audiobook chapters.

Log in at https://audobooks.covertsa.com once after the first start so Subsonic apps can connect. Default user is `admin` / `changeme` until you change it.

## Update

```bash
cd ~/audiobook-server
git pull
./install.sh
```

`users.json`, progress, playlists, covers, and other data files are not in git.
