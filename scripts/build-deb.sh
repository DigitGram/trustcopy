#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
#
# Builds trustcopy_<version>_all.deb from this checkout (only dpkg-deb needed, no debhelper):
#   scripts/build-deb.sh [OUTDIR]     default OUTDIR: the current folder
# The package installs for the whole system: sudo apt install ./trustcopy_<version>_all.deb
# The right-click entries live in each user's home, which a package cannot write to: the program creates them at its first start.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
OUT="$(mkdir -p "${1:-.}" && cd "${1:-.}" && pwd)"
VERSION="$(sed -n 's/^VERSION="\([^"]*\)".*/\1/p' "$ROOT/install.sh")"
[ -n "$VERSION" ] || { echo "no VERSION in install.sh" >&2; exit 1; }
APP_ID=io.github.digitgram.TrustCopy
EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --format=%ct 2>/dev/null || date +%s)}"

PKG="$(mktemp -d)"
trap 'rm -rf "$PKG"' EXIT
D="$PKG/trustcopy_${VERSION}_all"
mkdir -p "$D/DEBIAN" "$D/usr/bin" "$D/usr/share/applications" "$D/usr/share/doc/trustcopy"

# program files: the same layout as install.sh, below /usr/share/trustcopy
S="$D/usr/share/trustcopy"
install -Dm644 -t "$S" "$ROOT"/src/*.py
install -Dm644 -t "$S/po" "$ROOT"/po/*.po
chmod 755 "$S/trustcopy.py"
ln -s ../share/trustcopy/trustcopy.py "$D/usr/bin/trustcopy"

# desktop file and icons (the repo keeps icons as trustcopy.<ext>, they are installed under the app id)
sed -e 's|@LAUNCHER@|trustcopy|' -e "s|@APP_ID@|$APP_ID|" -e "s|@ICON@|$APP_ID|" "$ROOT/data/trustcopy.desktop.in" > "$D/usr/share/applications/$APP_ID.desktop"
chmod 644 "$D/usr/share/applications/$APP_ID.desktop"
(cd "$ROOT/data/icons/hicolor" && find . -type f | while read -r icon; do
  install -Dm644 "$icon" "$D/usr/share/icons/hicolor/$(dirname "$icon")/$APP_ID.${icon##*.}"
done)

# copyright (Debian wants it)
cat > "$D/usr/share/doc/trustcopy/copyright" <<'COPYRIGHT'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: TrustCopy
Source: https://github.com/DigitGram/trustcopy

Files: *
Copyright: 2026 DigitGram
License: GPL-3+

License: GPL-3+
 On Debian systems, the full text of the GNU General Public License version 3
 is in /usr/share/common-licenses/GPL-3.
COPYRIGHT
printf 'trustcopy (%s) unstable; urgency=medium\n\n  * Release %s, see https://github.com/DigitGram/trustcopy/releases\n\n -- DigitGram <120712067+DigitGram@users.noreply.github.com>  %s\n' \
  "$VERSION" "$VERSION" "$(LC_ALL=C date -u -d "@$EPOCH" '+%a, %d %b %Y %H:%M:%S +0000')" | gzip -9n > "$D/usr/share/doc/trustcopy/changelog.gz"

# DEBIAN/control
SIZE="$(du -sk --exclude=DEBIAN "$D" | cut -f1)"
cat > "$D/DEBIAN/control" <<CONTROL
Package: trustcopy
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5)
Recommends: nautilus | nemo | caja
Suggests: python3-nautilus
Maintainer: DigitGram <120712067+DigitGram@users.noreply.github.com>
Homepage: https://github.com/DigitGram/trustcopy
Description: Verified copying and moving for the Files right-click menu
 TrustCopy copies and moves files from the Scripts menu of Files (Nautilus,
 Nemo, Caja), reads every copy back and compares it, shows a live speed graph
 and asks what to do when a file already exists or fails. The right-click
 entries are created when TrustCopy is started for the first time.
CONTROL

# the right-click entries are the user's files: the first start of the program creates them, the removal of the package deletes
# those that carry our marker line (nothing else) in the home of every user
cat > "$D/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  /usr/bin/python3 -m compileall -q /usr/share/trustcopy >/dev/null 2>&1 || true
  command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
  command -v gtk4-update-icon-cache >/dev/null && gtk4-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi
exit 0
POSTINST
# the bytecode is not dpkg's file: it must go before dpkg removes the folders, or /usr/share/trustcopy stays behind
cat > "$D/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e
case "$1" in
  remove|upgrade|deconfigure) rm -rf /usr/share/trustcopy/__pycache__ ;;
esac
if [ "$1" = remove ]; then   # not on an upgrade: the entries stay and keep working
  MARKER='# Written by TrustCopy from its settings, replaced or deleted whenever they change'
  getent passwd | while IFS=: read -r _ _ uid _ _ home _; do
    [ "$uid" -ge 1000 ] 2>/dev/null && [ -d "$home" ] || continue
    for dir in "$home/.local/share/nautilus/scripts" "$home/.local/share/nemo/scripts" "$home/.config/caja/scripts"; do
      [ -d "$dir" ] || continue
      find "$dir" -maxdepth 2 -type f \( -name 'TrustCopy: *' -o -path "$dir/TrustCopy/*" \) 2>/dev/null | while IFS= read -r f; do
        [ "$(sed -n 2p "$f" 2>/dev/null)" = "$MARKER" ] && rm -f "$f"
      done
      rmdir "$dir/TrustCopy" 2>/dev/null || true
    done
  done
fi
exit 0
PRERM
cat > "$D/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e
case "$1" in
  remove|purge)
    command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
    command -v gtk4-update-icon-cache >/dev/null && gtk4-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor >/dev/null 2>&1 || true
    ;;
esac
exit 0
POSTRM

# same bytes for the same commit: fixed mtimes and modes, owner root
find "$D" -type d -exec chmod 755 {} +
find "$D" -type f ! -perm -u+x -exec chmod 644 {} +
chmod 755 "$D/DEBIAN/postinst" "$D/DEBIAN/prerm" "$D/DEBIAN/postrm"
find "$D" -exec touch -h -d "@$EPOCH" {} +
SOURCE_DATE_EPOCH="$EPOCH" dpkg-deb --root-owner-group -Zxz --build "$D" "$OUT/trustcopy_${VERSION}_all.deb" >/dev/null
echo "$OUT/trustcopy_${VERSION}_all.deb"
