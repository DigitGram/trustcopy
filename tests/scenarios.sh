#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DigitGram
# Sandbox to play through every TrustCopy dialog by hand.  Usage: tests/scenarios.sh [tamper]
set -eu
T=/tmp/trustcopy-test

if [ "${1:-}" = tamper ]; then  # run while a big file copies: breaks every attempt (TrustCopy retries 3 times), so the checksum must fail
  idle=0 limit=6000  # start this BEFORE the copy: it waits 60 s for it (a fast SSD is done in a second), then stops after 3 s without one
  echo "Waiting for a copy into $T/target ..."
  while [ $idle -lt $limit ]; do
    if f=$(ls "$T"/target/*.trustpart 2>/dev/null) && [ -s "$f" ]; then
      printf '\377' | dd of="$f" bs=1 conv=notrunc 2>/dev/null && echo "Changed the first byte of ${f##*/}"
      while [ -e "$f" ]; do sleep 0.01; done
      idle=0 limit=300
    fi
    sleep 0.01; idle=$((idle + 1))
  done
  exit
fi

rm -rf "$T"
mkdir -p "$T/source/folder/sub" "$T/target" "$T/target-readonly"
echo hello > "$T/source/hello.txt"
echo notes > "$T/source/notes.txt"
echo a > "$T/source/folder/a.txt"
echo b > "$T/source/folder/sub/b.txt"
truncate -s 600M "$T/source/big.bin"             # big enough to tamper with while it copies
mkdir "$T/huge"; truncate -s 4G "$T/huge/huge.bin"   # long enough to pause and cancel on an SSD; /tmp is often RAM, so copy it to a real disk
echo secret > "$T/source/locked.txt" && chmod 000 "$T/source/locked.txt"   # read error (not as root)
echo old > "$T/target/hello.txt" && touch -d 2001-01-01 "$T/target/hello.txt"   # conflict, older
chmod 555 "$T/target-readonly"                   # write error

cat <<EOF
Sandbox: $T   (select files in "Files", right-click > Scripts > TrustCopy)

 Everything OK     copy source/folder      to any new folder
 Conflict dialog   copy source/hello.txt   to target
 Failed file       copy source/*           anywhere  (locked.txt cannot be read)
 Bad checksum      first run:  $0 tamper   (it waits), then copy source/big.bin to target
 Retry failed      after "Bad checksum": let tamper end (3 s), then Retry failed: a new window, no conflict dialog, big.bin verified
 Write error       copy source/hello.txt   to target-readonly
 Disk full         sudo mount -t tmpfs -o size=20M tmpfs <new folder>; copy big.bin there
 Cancel / pause    copy huge/huge.bin to a folder on your SSD (not into $T: /tmp may be RAM), press the buttons; delete the copy afterwards
 Move              same as copy; the source must stay if anything failed
 Settings          verify off, other checksums, conflict policies, "Always close"
EOF
