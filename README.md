# Audiobook server

Single-file Python audiobook server for a Whatbox seedbox. No extra packages.

## Install

```bash
git clone https://github.com/djarvi-cloud/audiobook-server.git
cd audiobook-server
./install.sh
```

Books default to `~/files/audiobooks`. Override with `AUDIO_DIR` and `PORT` if needed:

```bash
AUDIO_DIR=~/files/audiobooks PORT=17930 ./install.sh
```

Log in at https://audobooks.covertsa.com once after the first start so Subsonic apps can connect. Default user is `admin` / `changeme` until you change it.

## Update

```bash
cd ~/audiobook-server
git pull
./install.sh
```

`users.json`, progress, covers, and other data files are not in git.
