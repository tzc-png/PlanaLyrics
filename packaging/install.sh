#!/usr/bin/env bash
# Install the launcher and icons so PlanaLyrics appears in the application grid
# and the dock like any other app, then refresh the desktop caches.
#
#   ./packaging/install.sh            install (or re-install after moving the folder)
#   ./packaging/install.sh remove     uninstall the launcher and icons
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
entry="$apps/planalyrics.desktop"

refresh() {
    update-desktop-database "$apps" >/dev/null 2>&1 || true
    gtk-update-icon-cache -f -t "$icons" >/dev/null 2>&1 || true
}

if [[ "${1:-}" == "remove" ]]; then
    rm -f "$entry"
    for dir in "$icons"/*/apps; do
        rm -f "$dir/planalyrics.png"
    done
    refresh
    echo "removed the PlanaLyrics launcher and icons"
    exit 0
fi

launcher="$root/planalyrics.py"
if [[ ! -f "$launcher" ]]; then
    echo "cannot find $launcher" >&2
    exit 1
fi
chmod +x "$launcher"

# icons first: a launcher with no icon looks broken
shopt -s nullglob
copied=0
for file in "$here"/icons/planalyrics-*.png; do
    size="$(basename "$file" .png)"
    size="${size##*-}"
    mkdir -p "$icons/${size}x${size}/apps"
    cp -f "$file" "$icons/${size}x${size}/apps/planalyrics.png"
    copied=$((copied + 1))
done
if [[ $copied -eq 0 ]]; then
    echo "no icons found — run packaging/make_icon.py first" >&2
    exit 1
fi

mkdir -p "$apps"
sed "s|@EXEC@|$launcher|" "$here/planalyrics.desktop.in" > "$entry"
refresh

echo "installed:"
echo "  $entry"
echo "  $icons/<size>x<size>/apps/planalyrics.png  ($copied sizes)"
echo
echo "PlanaLyrics should now appear in the application grid; if it does not,"
echo "log out and back in, or run 'gtk-launch planalyrics'."
