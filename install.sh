#!/usr/bin/env bash
# Phone Mirror installer: adds a launcher and app-grid entry for the current user.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.voidrlm.PhoneMirror

if ! python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gst", "1.0")
from gi.repository import Gtk, Adw, Gst
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6)
PY
then
    echo "Missing dependencies: PyGObject, GTK 4, libadwaita 1.6+ and GStreamer are required." >&2
    echo "  Fedora: sudo dnf install python3-gobject gtk4 libadwaita gstreamer1-plugins-bad-free" >&2
    exit 1
fi

if ! python3 -c "import dbus" 2>/dev/null; then
    echo "Note: controlling the phone needs python3-dbus:  sudo dnf install python3-dbus"
fi

if ! command -v uxplay >/dev/null; then
    echo "Note: the AirPlay receiver 'uxplay' is not installed yet."
    echo "      Install it with:  sudo dnf install uxplay   (or use the Install button in the app)"
fi

chmod +x "$here/phone-mirror"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sf "$here/phone-mirror" ~/.local/bin/phone-mirror
ln -sf "$here/$APP_ID.svg" ~/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg
sed "s|^Exec=.*|Exec=$HOME/.local/bin/phone-mirror|" "$here/$APP_ID.desktop" > ~/.local/share/applications/$APP_ID.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor 2>/dev/null || true
echo "Installed. Open “Phone Mirror” from the app grid, or run: phone-mirror"
