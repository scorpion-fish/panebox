#!/bin/bash
# Build the three PaneBox artifacts into dist/:
#   panebox_<ver>_all.deb            (dpkg-deb, payload at /usr/lib/panebox)
#   PaneBox-<ver>-x86_64.AppImage    (type-2 runtime + squashfs AppDir)
#   panebox_<ver>_amd64.snap         (classic-confinement squashfs + meta/)
#
# Prereqs on this host: dpkg-deb, mksquashfs, an existing AppImage to donate
# the type-2 runtime (default: ~/Downloads/Joplin-3.6.14.AppImage), python3+PIL
# for the icons (packaging/make_icons.py).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(cd "$ROOT" && python3 -c 'from panebox.constants import APP_VERSION; print(APP_VERSION)')"
OUT="$ROOT/dist"
BUILD="$ROOT/build/pkg"
ICONS="${ICONS:-$OUT/icons}"
RUNTIME_SRC="${RUNTIME_SRC:-$HOME/Downloads/Joplin-3.6.14.AppImage}"

echo "== PaneBox $VERSION =="
rm -rf "$BUILD"
mkdir -p "$OUT" "$BUILD/stage"

# ---- icons (regenerate when missing) ----------------------------------------
if [ ! -f "$ICONS/panebox.png" ]; then
    python3 "$ROOT/packaging/make_icons.py" "$ICONS"
fi

# ---- shared payload stage ----------------------------------------------------
STAGE="$BUILD/stage"
install -d "$STAGE/usr/lib/panebox"
cp -a "$ROOT/main.py" "$ROOT/panebox" "$ROOT/strings" "$ROOT/assets" "$STAGE/usr/lib/panebox/"
find "$STAGE" -name __pycache__ -type d -exec rm -rf {} +
install -D -m 755 "$ROOT/packaging/usr-bin-panebox" "$STAGE/usr/bin/panebox"
install -D -m 644 "$ROOT/packaging/panebox.desktop" "$STAGE/usr/share/applications/panebox.desktop"
for size in 512 256 128 64 48 32 24 16; do
    src="$ICONS/panebox.png"; [ "$size" != 512 ] && src="$ICONS/panebox-${size}x${size}.png"
    install -D -m 644 "$src" "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/panebox.png"
done
install -D -m 644 "$ROOT/README.md" "$STAGE/usr/share/doc/panebox/README.md"
sed "s/PLACEHOLDER_VERSION/$VERSION/" "$ROOT/packaging/debian/copyright" \
    > "$STAGE/usr/share/doc/panebox/copyright"

# ---- .deb --------------------------------------------------------------------
DEBDIR="$BUILD/debroot"
cp -a "$STAGE" "$DEBDIR"
mkdir "$DEBDIR/DEBIAN"
sed "s/PLACEHOLDER_VERSION/$VERSION/" "$ROOT/packaging/debian/control" > "$DEBDIR/DEBIAN/control"
install -m 755 "$ROOT/packaging/debian/postinst" "$DEBDIR/DEBIAN/postinst"
dpkg-deb --build --root-owner-group "$DEBDIR" "$OUT/panebox_${VERSION}_all.deb"
echo "built deb: $OUT/panebox_${VERSION}_all.deb"

# ---- .AppImage ----------------------------------------------------------------
APPDIR="$BUILD/appdir"
mkdir -p "$APPDIR/usr"
cp -a "$STAGE/usr/." "$APPDIR/usr/"
install -m 644 "$ROOT/packaging/panebox.desktop" "$APPDIR/panebox.desktop"
install -m 644 "$ICONS/panebox.png" "$APPDIR/panebox.png"
install -m 644 "$ICONS/panebox.png" "$APPDIR/.DirIcon"
install -m 755 "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
rm -f "$BUILD/app.squashfs"
mksquashfs "$APPDIR" "$BUILD/app.squashfs" -noappend -root-owned -no-recovery \
    -no-xattrs -no-duplicates -all-root >/dev/null

# Type-2 runtime: everything before the embedded squashfs magic ("hsqs"),
# truncated to the runtime's own ELF section-table end (that is the offset
# the runtime computes for itself at exec).
python3 - "$RUNTIME_SRC" "$BUILD/runtime" <<'PY'
import struct, sys
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
if data[:5] != b"\x7fELF\x02":
    raise SystemExit("runtime source is not an ELF64 AppImage")
e_shoff, = struct.unpack_from("<Q", data, 0x28)
e_shentsize, e_shnum = struct.unpack_from("<HH", data, 0x3A)
elf_size = e_shoff + e_shentsize * e_shnum
# The runtime ELF also contains the literal "hsqs" (it compares against it),
# so the embedded squashfs is the first magic AT OR AFTER the ELF end.
off = data.find(b"hsqs", elf_size)
assert off >= elf_size, "no embedded squashfs found after the runtime ELF"
open(dst, "wb").write(data[:elf_size])
print(f"runtime: {elf_size} bytes (squashfs at {off}, {off - elf_size} pad bytes skipped)")
PY
cat "$BUILD/runtime" "$BUILD/app.squashfs" > "$OUT/PaneBox-${VERSION}-x86_64.AppImage"
chmod +x "$OUT/PaneBox-${VERSION}-x86_64.AppImage"
echo "built appimage: $OUT/PaneBox-${VERSION}-x86_64.AppImage"

# ---- .snap --------------------------------------------------------------------
SNAPDIR="$BUILD/snaproot"
mkdir -p "$SNAPDIR"
cp -a "$STAGE/usr" "$SNAPDIR/usr"
install -d "$SNAPDIR/meta/gui"
sed "s/PLACEHOLDER_VERSION/$VERSION/" "$ROOT/packaging/snap/snap.yaml" > "$SNAPDIR/meta/snap.yaml"
# snap desktop integration: Exec is rewritten to the snap command name
sed "s/^Exec=panebox.*/Exec=panebox panebox/" "$ROOT/packaging/panebox.desktop" \
    > "$SNAPDIR/meta/gui/panebox.desktop"
install -m 644 "$ICONS/panebox-256x256.png" "$SNAPDIR/meta/gui/panebox.png"
rm -f "$BUILD/snap.squashfs"
mksquashfs "$SNAPDIR" "$BUILD/snap.squashfs" -noappend -root-owned -no-recovery \
    -no-xattrs -all-root >/dev/null
mv "$BUILD/snap.squashfs" "$OUT/panebox_${VERSION}_amd64.snap"
echo "built snap: $OUT/panebox_${VERSION}_amd64.snap"

rm -rf "$BUILD"
ls -lh "$OUT" | grep -E "panebox_|PaneBox-"
