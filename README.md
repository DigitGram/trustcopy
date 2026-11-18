<p align="center">
  <img src="docs/banner.png" alt="TrustCopy: copies you can trust, from the GNOME Files context menu">
</p>

# TrustCopy

[![tests](https://github.com/DigitGram/trustcopy/actions/workflows/test.yml/badge.svg)](https://github.com/DigitGram/trustcopy/actions/workflows/test.yml)

**Copies you can trust.** Fast, verified copying and moving from the right-click menu of GNOME Files (Nautilus),
inspired by the Windows tool TeraCopy. Sister project of [HashTally](https://github.com/DigitGram/hashtally).

Normal copying assumes the copy is fine. TrustCopy checks: every file is hashed while it is read, then the
copy is flushed, read back from the disk and compared, so a bad copy is reported instead of discovered
months later.

<p align="center">
  <img src="docs/screenshots/running-light.png" width="48%" alt="TrustCopy while copying: steps, live speed graph and progress">
  <img src="docs/screenshots/done-light.png" width="48%" alt="TrustCopy when done: all files verified, with write and read speed">
</p>

## What you get

- **Verified copies.** Each copy is read back from the disk and compared; a bad one never replaces your old file and is reported. Choose CRC32 (fast, default) or SHA-256.
- **Safe moves.** A source is deleted only after its own copy has been verified.
- **A live view.** Three steps (Copying, Verifying, Done), a speed graph, time remaining, and a file list with a status per file.
- **No half-written files.** A cancel or a power cut never leaves a damaged file under its real name.
- **A checksum file, if you want one.** Tick *Checksum file* in the window (or switch it on in the settings) and the target folder gets a `SHA256SUMS` (or SHA-512, SHA-1, MD5, SFV) listing every copied file, ready for [HashTally](https://github.com/DigitGram/hashtally) or `sha256sum -c`.
- **Conflicts handled your way.** Ask each time (keep both, skip, overwrite if newer, overwrite), or set a rule.
- **Pause, resume, queue.** Jobs run one after the other; errors are retried, and "Retry failed" repeats only those files.
- **Native look.** GTK 4 and libadwaita, light and dark, in ten languages (English, Deutsch, Español, Français, Italiano, 日本語, Polski, Português (Brasil), Українська, 简体中文).

<p align="center">
  <img src="docs/screenshots/conflict-light.png" width="48%" alt="The dialog for a file that already exists">
  <img src="docs/screenshots/settings-light.png" width="48%" alt="The settings window">
</p>

## Requirements

- A GNOME-family desktop with **Nautilus** (Files), for example Ubuntu 24.04 or newer, Fedora 40 or newer, Debian 13 or newer, Linux Mint 22 or newer. **Nemo** (Cinnamon) and **Caja** (MATE), when installed, get the same Scripts entries.
- Python 3.10+ as the system's `/usr/bin/python3`, GTK 4 + libadwaita >= 1.5 and pycairo for Python
  - Debian/Ubuntu: `sudo apt install python3-gi python3-gi-cairo gir1.2-adw-1`
  - Fedora: `sudo dnf install python3-gobject libadwaita`
  - Arch: `sudo pacman -S python-gobject libadwaita`

Older systems are not supported; the installer stops with an install hint.

## Install

One line (installs for the current user only, no root):

```sh
curl -fsSL https://github.com/DigitGram/trustcopy/releases/latest/download/install.sh | bash

# no curl (e.g. a fresh Ubuntu)? wget does the same:
wget -qO- https://github.com/DigitGram/trustcopy/releases/latest/download/install.sh | bash
```

Prefer to read the script first? Download it, inspect it, then run it:

```sh
curl -fsSLO https://github.com/DigitGram/trustcopy/releases/latest/download/install.sh
less install.sh && bash install.sh
```

Or from a checkout: `git clone https://github.com/DigitGram/trustcopy && cd trustcopy && ./install.sh`.
Running the one-line installer of a newer release updates an existing installation. If the entries do not show up,
restart the file manager (`nautilus -q`, `nemo -q` or `caja -q`); log out and in once if the launcher or icon is missing.

To remove it:

```sh
~/.local/share/trustcopy/install.sh --uninstall   # your settings in ~/.config/trustcopy are kept
~/.local/share/trustcopy/install.sh --purge       # removes the settings too
```

### Verify the download

Releases are signed with the key of `digitgram@proton.me`, fingerprint
`E7AA EB31 A4DE CF99 B314 7E1D FAAB 30B5 FD49 0E93`.

```sh
curl -fsSL https://github.com/DigitGram.gpg | gpg --import
curl -fsSLO https://github.com/DigitGram/trustcopy/releases/latest/download/install.sh
curl -fsSLO https://github.com/DigitGram/trustcopy/releases/latest/download/install.sh.asc
gpg --verify install.sh.asc install.sh
```

Run the script only if gpg reports a good signature and the fingerprint matches the one above. The signed script
names the exact commit it downloads, so the signature covers the program as well, not just the installer.

## Usage

Files cannot be taken over: normal Ctrl+C / Ctrl+V still uses Nautilus' own copier. TrustCopy adds entries to the
right-click menu under **Scripts**:

| Where | Entry |
| --- | --- |
| Right-click selected files | **Scripts** > **TrustCopy: Copy to…** / **TrustCopy: Move to…** (asks for the folder) |
| Right-click the folder background | **Scripts** > **TrustCopy: Paste from clipboard** (takes what you copied or cut with Ctrl+C / Ctrl+X) |

On some drives Files does not offer **Scripts** in the background menu (seen on the top folder of an exFAT USB stick).
Use a keyboard shortcut for pasting there.

**Keyboard shortcuts** (Nautilus only, off by default, switched on in the settings): **Ctrl+Alt+C** copy to…,
**Ctrl+Alt+X** move to…, **Ctrl+Alt+V** paste from clipboard. Each key can be changed there; a key that GNOME, Files or
another of your script shortcuts already uses is refused.

Language, checksum, conflict rule, *Reset settings* and *About* are in the settings (open **TrustCopy** from the Activities
overview; while a job runs, right-click its icon > *Settings*, or use the window's menu).

### Command line

From a terminal, `copy` and `move` with `--to` answer there, as text. A file that already exists is asked about in the
terminal (or give `--on-conflict skip|keep_both|newer|overwrite` for scripts). `-q` leaves out the banner and the files that
are fine, `--no-verify` skips the check, `--gui` forces the window.

```sh
trustcopy copy --to /media/maria/USB-Stick ~/Pictures/Holiday   # copy the folder Holiday onto the USB stick, verified
trustcopy move --to ~/Archive big.iso notes.txt                 # move two files; each source goes after its own check
trustcopy copy photo.jpg                                        # no --to: a dialog asks for the destination folder
trustcopy paste --to .                                          # what you copied or cut with Ctrl+C / Ctrl+X, into the current folder
trustcopy settings                                              # open the settings
```

The destination is always the folder after `--to`, and everything else on the line is a source. The folder is copied *into*
the destination: `~/Pictures/Holiday` ends up as `/media/maria/USB-Stick/Holiday`.
Exit code: 0 done, 1 a file failed or the job was cancelled, 2 the command could not run.

### Good to know

- Linux only. Symlinks are copied as links; devices, sockets and pipes are not copied (listed as failed).
- Ownership is not preserved (like plain `cp`); permissions, times and xattrs are.
- After a hard stop (crash, power cut) a `name.trustpart` can be left behind. After the next copy into that folder the window offers to delete such leftovers, and `trustcopy clean FOLDER` does it from a terminal (it lists them first and asks; `--yes` skips the question).
- Read-back verification cannot catch a cheap USB stick whose controller confirms a flush it has not done. For a copy that must
  not fail there: eject, plug it in again and check it with a hash file ([HashTally](https://github.com/DigitGram/hashtally)).

How verification, the speed graph and the file list work, and the full list of limits: [docs/details.md](docs/details.md).

## Translating

Translations are plain gettext `.po` files in `po/`, read directly at runtime. German is maintained by the author.
All other translations were generated by an AI assistant and have not been reviewed by native speakers;
corrections are welcome as pull requests. How to add a language: [docs/development.md](docs/development.md#translating).

## Development

Project layout, how it works, running the tests and trying every dialog by hand: [docs/development.md](docs/development.md).

## How it was made

TrustCopy is developed and maintained by DigitGram. The project is shaped by its requirements, design decisions and ongoing development.

AI-assisted tools were used during implementation and to support code quality improvements, testing, hardening and translations. All changes are reviewed and tested by the author, who is responsible for the final result.

The code is open, the test suite is available in `tests/`, and issues and pull requests are welcome.

## Support

TrustCopy is free and stays free. If it is useful to you, you can support its development on [Liberapay](https://liberapay.com/DigitGram). Donations are voluntary and unlock nothing.

## License

Copyright (C) 2026 DigitGram. TrustCopy is free software: you can redistribute it and/or modify it
under the terms of the [GNU General Public License](LICENSE) as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later version. It comes without
any warranty. The icon is part of the project and under the same license.
