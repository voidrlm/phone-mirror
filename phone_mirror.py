"""Phone Mirror: show an iPhone or iPad screen on this computer over AirPlay.

The AirPlay protocol itself is handled by UxPlay (https://github.com/FDH2/UxPlay),
which runs as a child process.  UxPlay forwards the phone's H.264 video to a local
UDP port as RTP; this app decodes it with GStreamer and draws it inside its own
window.  Audio is played by UxPlay directly.
"""

import collections
import json
import os
import pty
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk, Pango  # noqa: E402

try:
    import hid_remote
except ImportError:  # python3-dbus is missing: mirroring still works, control does not
    hid_remote = None

APP_ID = "io.github.voidrlm.PhoneMirror"
APP_NAME = "Phone Mirror"
VERSION = "1.0"

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "phone-mirror")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

RESOLUTIONS = [
    ("1280x720", "Low (720p)"),
    ("1920x1080", "Standard (1080p)"),
    ("2560x1440", "High (1440p)"),
]

RE_ACCEPT = re.compile(r"Accepted IPv[46] client on socket (\d+)")
RE_CLOSE = re.compile(r"(?:Connection closed on|Removing connection for) socket (\d+)")
RE_REQUEST = re.compile(r"connection request from (.+?) \((.*?)\) with deviceID")

CSS = """
.mirror-view { background-color: black; }
.pin-code { font-size: 2.6em; font-weight: 800; letter-spacing: 0.25em; }
.mirror-view:focus, .mirror-view:focus-visible { outline: none; }
.log-view { font-family: monospace; font-size: 0.9em; }
"""


def default_name():
    host = GLib.get_host_name().split(".")[0]
    if not host or host == "localhost":
        return "Linux PC"
    return host[:1].upper() + host[1:]


DEFAULTS = {
    "name": default_name(),
    "resolution": "1920x1080",
    "fps60": True,
    "low_latency": True,
    "compatible_decoder": False,
    "audio": True,
    "pin": False,
    "separate_window": False,
    "control": False,
    "trackpad": False,
    "touch_scroll": True,
    "control_help_seen": False,
}

RECEIVER_KEYS = ("name", "resolution", "fps60", "low_latency", "audio", "pin", "separate_window")
MOUSE_BUTTONS = {1: 1, 3: 2, 2: 4}  # GDK button -> HID button bit


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for key, default in DEFAULTS.items():
            if isinstance(saved.get(key), type(default)):
                cfg[key] = saved[key]
    except (OSError, ValueError, AttributeError):
        pass
    if cfg["resolution"] not in [r for r, _ in RESOLUTIONS]:
        cfg["resolution"] = DEFAULTS["resolution"]
    cfg["name"] = cfg["name"].strip()[:40] or DEFAULTS["name"]
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]



def uxplay_install_command():
    """The command that installs uxplay with this system's package manager, or None (e.g. Arch: AUR)."""
    for manager, cmd in (("dnf", ["dnf", "install", "-y"]), ("apt-get", ["apt-get", "install", "-y"])):
        if shutil.which(manager):
            return cmd + ["uxplay"]
    return None

class Receiver:
    """A running UxPlay process whose output is delivered line by line."""

    def __init__(self, cmd, cwd, on_line, on_exit):
        self.on_line = on_line
        self.on_exit = on_exit
        self.stopped = False
        self._buf = b""
        # A pty makes UxPlay line-buffer its output, so events arrive promptly.
        master, slave = pty.openpty()
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._fd = master
        self._watch = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            master,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._ready,
        )

    def _ready(self, fd, _cond):
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if data:
            self._buf += data
            *lines, self._buf = self._buf.split(b"\n")
            for raw in lines:
                self.on_line(self, raw.decode("utf-8", "replace").rstrip("\r"))
            return True
        self._watch = 0
        os.close(fd)
        try:
            code = self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        self.on_exit(self, code)
        return False

    def stop(self):
        self.stopped = True
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
            os.close(self._fd)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=440, default_height=700)
        self.set_icon_name(APP_ID)
        self.set_size_request(240, 240)  # libadwaita's default minimum (360) is wider than a phone picture
        self.cfg = load_config()
        self.log_lines = collections.deque(maxlen=3000)
        self.receiver = None
        self.server_state = "stopped"  # stopped | running | missing | installing | failed
        self.pin = None
        self.sockets = set()
        self.device = None
        self.mirroring = False
        self.have_frame = False
        self.pipeline = None
        self.paintable = None
        self.udp_port = 0
        self._fit_aspect = None
        self._idle_timer = 0
        self._inhibit = 0
        self._button_action = None
        self.remote = None
        self._accels = {}
        self._keys_captured = False
        self._last_pointer = None
        self._decoder = ""
        self._frames_in = 0
        self._watchdog = 0
        self._snap_timer = 0
        self._sized = None

        self._build_ui()
        self._build_pipeline()
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreened)
        self.connect("notify::default-width", self._on_resized)
        self.connect("notify::default-height", self._on_resized)
        self.start_server()
        if self.cfg["control"] and hid_remote:
            self.control_button.set_active(True)

    # ---- interface -----------------------------------------------------------

    def _build_ui(self):
        for name, callback, accels in [
            ("fullscreen", self._act_fullscreen, ["F11"]),
            ("screenshot", self._act_screenshot, ["<Control>s"]),
            ("preferences", self._act_preferences, ["<Control>comma"]),
            ("restart", lambda *_: self.start_server(), ["<Control>r"]),
            ("log", self._act_log, []),
            ("about", self._act_about, []),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self._accels[f"win.{name}"] = accels
        self._accels["app.quit"] = ["<Control>q"]
        self._apply_accels()

        self.title_widget = Adw.WindowTitle(title=APP_NAME)
        header = Adw.HeaderBar(title_widget=self.title_widget)
        menu = Gio.Menu()
        menu.append("Save Screenshot", "win.screenshot")
        menu.append("Preferences", "win.preferences")
        menu.append("Restart Receiver", "win.restart")
        menu.append("Show Log", "win.log")
        menu.append(f"About {APP_NAME}", "win.about")
        header.pack_end(
            Gtk.MenuButton(
                icon_name="open-menu-symbolic", menu_model=menu, primary=True, tooltip_text="Main Menu"
            )
        )
        self.fs_button = Gtk.Button(
            icon_name="view-fullscreen-symbolic",
            tooltip_text="Fullscreen (F11)",
            action_name="win.fullscreen",
        )
        header.pack_end(self.fs_button)
        self.control_button = Gtk.ToggleButton(
            icon_name="input-mouse-symbolic",
            tooltip_text="Control the Phone from This Computer"
            if hid_remote
            else "Control needs the python3-dbus package",
            sensitive=bool(hid_remote),
        )
        self.control_button.connect("toggled", self._on_control_toggled)
        header.pack_start(self.control_button)

        self.pin_label = Gtk.Label(css_classes=["pin-code", "numeric"], visible=False)
        self.spinner = Adw.Spinner(width_request=32, height_request=32, visible=False)
        self.action_button = Gtk.Button(
            css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER, visible=False
        )
        self.action_button.connect("clicked", lambda *_: self._button_action and self._button_action())
        extras = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, halign=Gtk.Align.CENTER)
        extras.append(self.pin_label)
        extras.append(self.spinner)
        extras.append(self.action_button)
        self.status = Adw.StatusPage(child=extras)

        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.add_css_class("mirror-view")
        self.picture.set_focusable(True)
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_picture_pressed)
        click.connect("released", self._on_picture_released)
        click.connect("cancel", lambda *_: self.remote and self.remote.release_all())
        self.picture.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_pointer_motion)
        motion.connect("motion", self._on_pointer_motion)
        motion.connect("leave", self._on_pointer_leave)
        self.picture.add_controller(motion)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_picture_scroll)
        scroll.connect("scroll-end", lambda *_: self.remote and self.remote.swipe_end())
        self.picture.add_controller(scroll)
        phone_keys = Gtk.EventControllerKey()
        phone_keys.connect("key-pressed", self._on_phone_key, True)
        phone_keys.connect("key-released", self._on_phone_key, False)
        self.picture.add_controller(phone_keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self._update_key_capture())
        focus.connect("leave", lambda *_: self._update_key_capture())
        self.picture.add_controller(focus)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=150,
            hhomogeneous=False,
            vhomogeneous=False,
        )
        self.stack.add_named(self.status, "status")
        self.stack.add_named(self.picture, "video")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.view = Adw.ToolbarView(content=self.toasts)
        self.view.add_top_bar(header)

        self.control_label = Gtk.Label(
            xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption"], margin_start=6
        )
        self.control_bar = Gtk.Box(spacing=2, css_classes=["toolbar"], visible=False)
        self.control_bar.append(self.control_label)
        self.control_keys = []
        for icon, tip, callback in [
            ("go-home-symbolic", "Home", lambda *_: self.remote.tap(hid_remote.USAGE_HOME)),
            ("view-grid-symbolic", "App Switcher", lambda *_: self.remote.tap(hid_remote.USAGE_HOME, 2)),
            ("input-keyboard-symbolic", "Show or Hide the Phone’s Keyboard", lambda *_: self.remote.tap(hid_remote.USAGE_EJECT)),
            ("system-lock-screen-symbolic", "Lock the Phone", lambda *_: self.remote.tap(hid_remote.USAGE_POWER)),
        ]:
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"], focus_on_click=False)
            button.connect("clicked", callback)
            self.control_bar.append(button)
            self.control_keys.append(button)
        help_button = Gtk.Button(
            icon_name="help-about-symbolic", tooltip_text="How to Set Up Control", css_classes=["flat"]
        )
        help_button.connect("clicked", lambda *_: self._show_control_help())
        self.control_bar.append(help_button)
        self.view.add_bottom_bar(self.control_bar)
        self.set_content(self.view)
        self.connect("notify::is-active", lambda *_: self._update_key_capture())

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def _set_status(self, icon, title, description="", pin=None, busy=False, button=None, action=None):
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(description)
        self.pin_label.set_visible(bool(pin))
        self.pin_label.set_label(pin or "")
        self.spinner.set_visible(busy)
        self.action_button.set_visible(bool(button))
        self.action_button.set_label(button or "")
        self._button_action = action

    @property
    def embedded(self):
        return self.pipeline is not None and not self.cfg["separate_window"]

    def _update_ui(self):
        esc = GLib.markup_escape_text
        name = self.cfg["name"]
        device = self.device or "your device"
        show_video = self.server_state == "running" and self.embedded and self.have_frame
        subtitle = ""

        if self.server_state == "missing":
            can_install = bool(shutil.which("pkexec") and uxplay_install_command())
            self._set_status(
                "dialog-warning-symbolic",
                "AirPlay Receiver Not Installed",
                "Phone Mirror needs the “uxplay” package to talk to your iPhone."
                + ("" if can_install else "\n\nInstall it with your package manager, then try again."),
                button="Install" if can_install else "Try Again",
                action=self._install_uxplay if can_install else self.start_server,
            )
        elif self.server_state == "installing":
            self._set_status("folder-download-symbolic", "Installing…", "Setting up the AirPlay receiver.", busy=True)
        elif self.server_state == "failed":
            tail = [line for line in list(self.log_lines)[-6:] if line.strip()]
            hint = ""
            if any("dns" in line.lower() or "avahi" in line.lower() for line in tail):
                hint = "The network discovery service (Avahi) may not be running.\n\n"
            self._set_status(
                "dialog-error-symbolic",
                "Receiver Stopped",
                hint + esc("\n".join(tail[-4:])),
                button="Try Again",
                action=self.start_server,
            )
        elif show_video:
            subtitle = f"Mirroring {device}"
        elif self.mirroring and not self.embedded:
            subtitle = f"Mirroring {device}"
            self._set_status(
                "video-display-symbolic",
                f"Mirroring {esc(device)}",
                "The screen is shown in a separate window.",
            )
        elif self.mirroring:
            subtitle = f"Connected to {device}"
            self._set_status("phone-apple-iphone-symbolic", "Starting Video…", esc(device), busy=True)
        elif self.device:
            subtitle = f"Connected to {device}"
            self._set_status(
                "phone-apple-iphone-symbolic",
                f"Connected to {esc(device)}",
                "Enter the code below on the device if it asks for one."
                if self.pin
                else "Waiting for the screen or audio stream…",
                pin=self.pin,
            )
        else:
            subtitle = f"Visible as “{name}”"
            self._set_status(
                "phone-apple-iphone-symbolic",
                "Ready to Mirror",
                "On your iPhone or iPad, open <b>Control Center</b>, tap "
                f"<b>Screen Mirroring</b> and choose <b>{esc(name)}</b>.\n\n"
                "Both devices need to be on the same Wi‑Fi network."
                + ("\n\nEnter this code on the device when asked:" if self.pin else ""),
                pin=self.pin,
            )

        self.title_widget.set_subtitle(subtitle)
        self.stack.set_visible_child_name("video" if show_video else "status")
        self.lookup_action("screenshot").set_enabled(show_video)

        active = show_video or self.mirroring
        app = self.get_application()
        if active and not self._inhibit:
            self._inhibit = app.inhibit(self, Gtk.ApplicationInhibitFlags.IDLE, "Mirroring a phone screen")
        elif not active and self._inhibit:
            app.uninhibit(self._inhibit)
            self._inhibit = 0
        self._update_control_ui()

    def toast(self, text, button=None, callback=None):
        toast = Adw.Toast(title=text, timeout=4)
        if button:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: callback())
        self.toasts.add_toast(toast)

    # ---- embedded video ------------------------------------------------------

    def _build_pipeline(self):
        if not Gst.ElementFactory.find("gtk4paintablesink"):
            self._log("gtk4paintablesink is not available; using a separate video window")
            return
        self.udp_port = self.udp_port or free_udp_port()
        # openh264dec hands every frame over the moment it is decoded.  The hardware
        # decoder (what decodebin picks) always holds one frame back, i.e. 17-33 ms.
        fast = self.cfg["low_latency"] and not self.cfg["compatible_decoder"]
        if fast and Gst.ElementFactory.find("openh264dec"):
            self._decoder = "openh264dec"
            decode = "openh264dec ! gtk4paintablesink name=sink sync=false"
        else:
            self._decoder = "decodebin"
            decode = "decodebin name=dec dec. ! gtk4paintablesink name=sink sync=false"
        description = (
            f"udpsrc address=127.0.0.1 port={self.udp_port} buffer-size=4194304 "
            'caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=H264,payload=96" '
            "! queue max-size-buffers=0 max-size-bytes=0 max-size-time=0 "
            "! rtph264depay name=depay ! h264parse ! " + decode
        )
        try:
            pipeline = Gst.parse_launch(description)
        except GLib.Error as err:
            self._log(f"could not build the video pipeline: {err.message}")
            return
        self.pipeline = pipeline
        self.paintable = pipeline.get_by_name("sink").props.paintable
        self.paintable.connect("invalidate-contents", self._on_frame)
        self.paintable.connect("invalidate-size", lambda *_: self._fit_window())
        self.picture.set_paintable(self.paintable)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_pipeline_error)
        self._frames_in = 0
        pipeline.get_by_name("depay").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count_frame)
        if self._decoder == "openh264dec" and not self._watchdog:
            self._watchdog = GLib.timeout_add(1000, self._check_decoder)
        pipeline.set_state(Gst.State.PLAYING)

    def _count_frame(self, _pad, _info):  # runs on a GStreamer thread
        self._frames_in += 1
        return Gst.PadProbeReturn.OK

    def _check_decoder(self):
        """If video data keeps arriving but the fast decoder shows nothing, stop using it."""
        if self._decoder != "openh264dec":
            self._watchdog = 0
            return GLib.SOURCE_REMOVE
        if self._frames_in > 45 and not self.have_frame:
            self._watchdog = 0
            self._use_compatible_decoder("the fast decoder produced no picture")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _use_compatible_decoder(self, reason):
        self._log(f"video: {reason}; switching to the hardware decoder")
        self.cfg["compatible_decoder"] = True
        self._save_config()
        self.have_frame = False
        self._fit_aspect = None
        self._restart_pipeline()
        self.start_server()  # the phone has to send a fresh stream for the new decoder
        self.toast("Switched video decoder. Start Screen Mirroring on the phone again.")

    def _drop_pipeline(self):
        if self.pipeline:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.paintable = None

    def _restart_pipeline(self):
        """Start from a clean decoder so the next stream is picked up from its first frame."""
        if not self.pipeline:
            return
        self._drop_pipeline()
        self._build_pipeline()
        if not self.pipeline and self.server_state == "running":
            self.start_server()  # fall back to UxPlay's own window

    def _on_frame(self, paintable):
        if self.have_frame or paintable is not self.paintable or paintable.get_intrinsic_width() <= 0:
            return
        self.have_frame = True
        self._update_ui()
        self._fit_window()

    def _on_pipeline_error(self, _bus, message):
        err, debug = message.parse_error()
        self._log(f"video pipeline error: {err.message} ({debug})")
        if self._decoder == "openh264dec":
            self._use_compatible_decoder("the fast decoder reported an error")
            return
        self.have_frame = False
        self._fit_aspect = None
        self._restart_pipeline()
        self._update_ui()
        self.toast("Video could not be shown. Try “Use separate video window” in Preferences.")

    def _video_aspect(self):
        if not (self.have_frame and self.paintable):
            return None
        width, height = self.paintable.get_intrinsic_width(), self.paintable.get_intrinsic_height()
        return width / height if width > 0 and height > 0 else None

    def _floating(self):
        """True when the window is free to take any size (not fullscreen, maximized or tiled)."""
        if self.is_fullscreen() or self.is_maximized():
            return False
        surface = self.get_surface()
        return not (surface and surface.get_state() & Gdk.ToplevelState.TILED)

    def _size_limits(self):
        """(bars height, smallest content width, largest content width, largest content height)"""
        screen_w, screen_h = 1920, 1080
        surface = self.get_surface()
        if surface:
            monitor = self.get_display().get_monitor_at_surface(surface)
            if monitor:
                geometry = monitor.get_geometry()
                screen_w, screen_h = geometry.width, geometry.height
        bars = self.view.get_top_bar_height() + self.view.get_bottom_bar_height()
        min_w = max(self.view.measure(Gtk.Orientation.HORIZONTAL, -1)[0], 200)
        return bars, min_w, screen_w - 40, screen_h - 56 - bars

    def _resize_to(self, content_w, aspect):
        """Give the video area exactly the phone's shape, so there are no black borders."""
        bars, min_w, max_w, max_h = self._size_limits()
        content_w = min(max(content_w, min_w), max_w, max(max_h * aspect, min_w))
        width = round(content_w)
        height = min(round(width / aspect), max_h) + bars
        self._sized = (width, height)
        self.set_default_size(width, height)

    def _fit_window(self):
        """Resize the window to the shape of the phone screen (portrait or landscape)."""
        aspect = self._video_aspect()
        if not aspect or not self._floating():
            return
        if self._fit_aspect and abs(aspect - self._fit_aspect) < 0.01:
            return
        self._fit_aspect = aspect
        bars, _min_w, max_w, max_h = self._size_limits()
        if aspect < 1:
            content_w = min(max_h - 40, 900) * aspect
        else:
            content_w = min(max_w * 0.7, 1280)
        self._resize_to(content_w, aspect)

    def _keep_shape(self):
        """Re-trim the window at its current width, e.g. after the control bar appeared."""
        aspect = self._video_aspect()
        if aspect and self._floating() and self.stack.get_visible_child_name() == "video":
            self._resize_to(self.get_width(), aspect)
        return GLib.SOURCE_REMOVE

    def _on_resized(self, *_):
        if self._snap_timer:
            GLib.source_remove(self._snap_timer)
        self._snap_timer = GLib.timeout_add(300, self._snap_to_video)

    def _snap_to_video(self):
        """After the window was resized by hand, trim it back to the phone's shape."""
        self._snap_timer = 0
        aspect = self._video_aspect()
        if not aspect or not self._floating() or self.stack.get_visible_child_name() != "video":
            return GLib.SOURCE_REMOVE
        width, height = self.get_width(), self.get_height()
        if self._sized and abs(width - self._sized[0]) <= 1 and abs(height - self._sized[1]) <= 1:
            return GLib.SOURCE_REMOVE
        bars = self.view.get_top_bar_height() + self.view.get_bottom_bar_height()
        if self._sized and abs(width - self._sized[0]) <= 1:
            width = (height - bars) * aspect  # only the height was dragged: follow it
        self._resize_to(width, aspect)
        return GLib.SOURCE_REMOVE

    # ---- receiver process ----------------------------------------------------

    def _log(self, line):
        self.log_lines.append(line)

    def _command(self):
        cfg = self.cfg
        cmd = ["uxplay", "-n", cfg["name"], "-nh"]
        if cfg["fps60"]:
            cmd += ["-s", cfg["resolution"] + "@60", "-fps", "60"]
        else:
            cmd += ["-s", cfg["resolution"]]
        if not cfg["audio"]:
            cmd += ["-as", "0"]
        if cfg["pin"]:
            self.pin = f"{1000 + secrets.randbelow(9000)}"
            cmd += ["-pin", self.pin, "-reg", os.path.join(CONFIG_DIR, "paired-devices")]
        else:
            self.pin = None
        if cfg["low_latency"]:
            cmd += ["-vsync", "no"]  # play picture and sound the moment they arrive
        if self.embedded:
            sink = f"config-interval=1 ! udpsink host=127.0.0.1 port={self.udp_port}"
            cmd += ["-vrtp", sink + (" sync=false async=false" if cfg["low_latency"] else "")]
        if shutil.which("setpriv"):  # make sure UxPlay never outlives this app
            cmd = ["setpriv", "--pdeathsig", "TERM"] + cmd
        return cmd

    def start_server(self):
        self.stop_server()
        self._end_session(update=False)
        if not shutil.which("uxplay"):
            self.server_state = "missing"
            self._update_ui()
            return
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cmd = self._command()
        self._log(f"--- {time.strftime('%H:%M:%S')} starting: {shlex.join(cmd)}")
        try:
            self.receiver = Receiver(cmd, CONFIG_DIR, self._on_line, self._on_exit)
            self.server_state = "running"
        except OSError as err:
            self._log(f"could not start uxplay: {err}")
            self.server_state = "failed"
        self._update_ui()

    def stop_server(self):
        if self.receiver:
            self.receiver.stop()
            self.receiver = None
        self.server_state = "stopped"

    def _on_exit(self, receiver, code):
        if receiver is not self.receiver or receiver.stopped:
            return
        self._log(f"--- uxplay exited with status {code}")
        self.receiver = None
        self.server_state = "failed"
        self._end_session()

    def _on_line(self, receiver, line):
        if receiver is not self.receiver:
            return
        self._log(line)
        changed = False

        match = RE_ACCEPT.search(line)
        if match:
            self.sockets.add(match.group(1))
            self._cancel_idle_timer()
        match = RE_CLOSE.search(line)
        if match:
            self.sockets.discard(match.group(1))
            if not self.sockets:
                self._start_idle_timer()
        match = RE_REQUEST.search(line)
        if match:
            self.device = match.group(1).strip() or None
            changed = True

        if "starting mirroring" in line:
            self.mirroring = True
            changed = True
        elif "running is no longer true" in line:
            self.mirroring = False
            if self.have_frame:
                self.have_frame = False
                self._fit_aspect = None
                self._restart_pipeline()
            changed = True
        elif "lost connection with client" in line or "Initialized server socket" in line:
            self.sockets.clear()
            self._end_session()
            return
        if changed:
            self._update_ui()

    def _cancel_idle_timer(self):
        if self._idle_timer:
            GLib.source_remove(self._idle_timer)
            self._idle_timer = 0

    def _start_idle_timer(self):
        self._cancel_idle_timer()
        self._idle_timer = GLib.timeout_add(500, self._on_idle_timer)

    def _on_idle_timer(self):
        self._idle_timer = 0
        if not self.sockets:
            self._end_session()
        return GLib.SOURCE_REMOVE

    def _end_session(self, update=True):
        """The phone disconnected: forget it and get ready for the next one."""
        self._cancel_idle_timer()
        had_video = self.have_frame
        self.sockets.clear()
        self.device = None
        self.mirroring = False
        self.have_frame = False
        self._fit_aspect = None
        if had_video:
            self._restart_pipeline()
            if self.is_fullscreen():
                self.unfullscreen()
        if update:
            self._update_ui()

    def _install_uxplay(self):
        self.server_state = "installing"
        self._update_ui()
        try:
            proc = Gio.Subprocess.new(["pkexec", *uxplay_install_command()], Gio.SubprocessFlags.NONE)
        except GLib.Error as err:
            self._log(f"could not start the installer: {err.message}")
            self.start_server()
            return
        proc.wait_async(None, lambda *_: self.start_server())

    # ---- actions -------------------------------------------------------------

    def _act_fullscreen(self, *_):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreened(self, *_):
        full = self.is_fullscreen()
        self.view.set_reveal_top_bars(not full)
        self.view.set_reveal_bottom_bars(not full)
        self.fs_button.set_icon_name("view-restore-symbolic" if full else "view-fullscreen-symbolic")

    def _on_key(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape and self.is_fullscreen() and not self._keys_captured:
            self.unfullscreen()
            return True
        return False

    # ---- controlling the phone ----------------------------------------------

    @property
    def controlling(self):
        return bool(self.remote and self.remote.connected and self.stack.get_visible_child_name() == "video")

    def _on_control_toggled(self, button):
        enabled = button.get_active()
        if enabled and not self.remote:
            self.remote = hid_remote.HidRemote(APP_NAME, self._on_remote_state, self._ask_pairing, self._log)
            self.remote.absolute = not self.cfg["trackpad"]
            self.remote.start()
            if self.remote and not self.cfg["control_help_seen"]:
                self._show_control_help()
            enabled = self.remote is not None  # starting can fail straight away
        elif not enabled and self.remote:
            remote, self.remote = self.remote, None
            remote.stop()
        if self.cfg["control"] != enabled:
            self.cfg["control"] = enabled
            self._save_config()
        self._update_control_ui()
        GLib.timeout_add(80, self._keep_shape)  # once the bar has appeared or gone

    def _on_remote_state(self):
        if self.remote and self.remote.state == "error":
            message = self.remote.error
            self.remote = None
            self.control_button.set_active(False)
            self.toast(f"Control is unavailable: {message}")
        self._update_control_ui()

    def _update_control_ui(self):
        remote = self.remote
        self.control_bar.set_visible(remote is not None)
        if remote:
            if remote.connected:
                text = "Controlling the phone · Ctrl works as ⌘" if self.controlling else "Control connected"
            elif remote.state == "waiting":
                text = "Waiting for the phone’s Bluetooth…"
            else:
                text = "Starting Bluetooth…"
            self.control_label.set_label(text)
            self.control_label.set_tooltip_text(text)
            for button in self.control_keys:
                button.set_sensitive(remote.connected)
        self._update_key_capture()

    def _update_key_capture(self):
        """While the video has keyboard focus, every key (except F11) goes to the phone."""
        capture = self.controlling and self.is_active() and self.picture.has_focus()
        if capture == self._keys_captured:
            return
        self._keys_captured = capture
        self._apply_accels()
        if not capture and self.remote:
            self.remote.release_all()

    def _apply_accels(self):
        app = self.get_application()
        for action, accels in self._accels.items():
            keep = not self._keys_captured or action == "win.fullscreen"
            app.set_accels_for_action(action, accels if keep else [])

    def _ask_pairing(self, device, respond):
        dialog = Adw.AlertDialog(
            heading="Allow Bluetooth Pairing?",
            body=f"“{device}” wants to pair with this computer so that it can be controlled from here.",
        )
        dialog.add_response("deny", "Deny")
        dialog.add_response("pair", "Pair")
        dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("pair")
        dialog.set_close_response("deny")
        dialog.connect("response", lambda _dialog, response: respond(response == "pair"))
        dialog.present(self)

    def _show_control_help(self):
        dialog = Adw.AlertDialog(
            heading="Control Your iPhone",
            body="This computer connects to the phone as a Bluetooth mouse and keyboard.\n\n"
            "<b>1.</b> On the iPhone turn on <b>Settings → Accessibility → Touch → AssistiveTouch</b>. "
            "The iPhone only shows a pointer and accepts clicks while this is on.\n\n"
            "<b>2.</b> If the bar at the bottom keeps waiting for Bluetooth, open <b>Settings → Bluetooth</b> "
            "on the iPhone and tap <b>Phone Mirror</b> "
            f"(it can also be listed as “{GLib.markup_escape_text(GLib.get_host_name())}”), "
            "then accept pairing on both sides.\n\n"
            "Click the mirrored screen, then click, drag, scroll and type. Right‑click is a long press "
            "and Ctrl works as ⌘ (Ctrl+H is Home). While control is on, the iPhone hides its own "
            "keyboard; the keyboard button below brings it back.",
            body_use_markup=True,
        )
        dialog.add_response("ok", "Got It")
        dialog.set_default_response("ok")
        dialog.present(self)
        if not self.cfg["control_help_seen"]:
            self.cfg["control_help_seen"] = True
            self._save_config()

    def _save_config(self):
        try:
            save_config(self.cfg)
        except OSError as err:
            self.toast(f"Could not save preferences: {err}")

    def _shown_size(self):
        """Size in widget pixels at which the phone screen is drawn (None if there is no video)."""
        width, height = self.picture.get_width(), self.picture.get_height()
        video_w = self.paintable.get_intrinsic_width() if self.paintable else 0
        video_h = self.paintable.get_intrinsic_height() if self.paintable else 0
        if min(width, height, video_w, video_h) <= 0:
            return None
        scale = min(width / video_w, height / video_h)
        return video_w * scale, video_h * scale

    def _video_point(self, x, y):
        """Widget coordinates -> position on the phone screen as fractions 0..1."""
        shown = self._shown_size()
        if not shown:
            return 0.5, 0.5
        width, height = self.picture.get_width(), self.picture.get_height()
        return (x - (width - shown[0]) / 2) / shown[0], (y - (height - shown[1]) / 2) / shown[1]

    def _on_pointer_motion(self, _controller, x, y):
        if not self.controlling:
            self._last_pointer = None
            return
        if self.cfg["trackpad"]:
            if self._last_pointer:
                self.remote.move_rel((x - self._last_pointer[0]) * 1.2, (y - self._last_pointer[1]) * 1.2)
        else:
            self.remote.move_abs(*self._video_point(x, y))
        self._last_pointer = (x, y)

    def _on_pointer_leave(self, *_):
        self._last_pointer = None

    def _on_picture_pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        if self.controlling:
            self.picture.grab_focus()
            if button in MOUSE_BUTTONS:
                if not self.cfg["trackpad"]:
                    self.remote.move_abs(*self._video_point(x, y))
                self.remote.button(MOUSE_BUTTONS[button], True)
        elif button == Gdk.BUTTON_PRIMARY and n_press == 2:
            self._act_fullscreen()

    def _on_picture_released(self, gesture, _n_press, _x, _y):
        button = gesture.get_current_button()
        if self.remote and button in MOUSE_BUTTONS:
            self.remote.button(MOUSE_BUTTONS[button], False)

    def _on_picture_scroll(self, controller, dx, dy):
        if not self.controlling:
            return False
        touchpad = controller.get_unit() == Gdk.ScrollUnit.SURFACE
        shown = self._shown_size()
        if self.cfg["touch_scroll"] and shown:
            # scroll the way a finger does: the page follows the drag
            self.remote.rel_scale = (shown[0] * 1.2, shown[1] * 1.2)
            if touchpad:
                self.remote.swipe(-dx / shown[0], -dy / shown[1], animate=False)
            else:
                self.remote.swipe(-dx * 0.25, -dy * 0.14, animate=True)
        else:
            self.remote.scroll(-dy / 25 if touchpad else -dy)
        return True

    def _on_phone_key(self, _controller, keyval, keycode, _state, pressed):
        if not self._keys_captured or keyval == Gdk.KEY_F11:
            return False
        self.remote.key(keycode - 8, pressed)
        return True

    def _act_screenshot(self, *_):
        if not (self.paintable and self.have_frame):
            return
        width = self.paintable.get_intrinsic_width()
        height = self.paintable.get_intrinsic_height()
        snapshot = Gtk.Snapshot()
        self.paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            return
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser(
            "~/Pictures"
        )
        folder = os.path.join(pictures, APP_NAME)
        path = os.path.join(folder, time.strftime("Phone %Y-%m-%d %H-%M-%S.png"))
        try:
            os.makedirs(folder, exist_ok=True)
            texture = self.get_renderer().render_texture(node, None)
            if not texture.save_to_png(path):
                raise OSError("could not write the image")
        except (OSError, GLib.Error) as err:
            self.toast(f"Screenshot failed: {err}")
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
        self.toast("Screenshot saved", "Open", lambda: launcher.launch(self, None, None))

    def _act_preferences(self, *_):
        cfg = self.cfg
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title="Receiver")
        name = Adw.EntryRow(title="Name shown on the iPhone", text=cfg["name"])
        quality = Adw.ComboRow(
            title="Video quality",
            subtitle="Higher is sharper but needs a stronger Wi‑Fi signal",
            model=Gtk.StringList.new([label for _, label in RESOLUTIONS]),
            selected=[r for r, _ in RESOLUTIONS].index(cfg["resolution"]),
        )
        fps = Adw.SwitchRow(title="60 frames per second", subtitle="Smoother motion", active=cfg["fps60"])
        audio = Adw.SwitchRow(
            title="Play phone audio", subtitle="Sound from the phone comes out of this computer", active=cfg["audio"]
        )
        low_latency = Adw.SwitchRow(
            title="Lowest delay",
            subtitle="Show picture and play sound the instant they arrive. "
            "Turn off if the video stutters or sound drifts from the picture",
            active=cfg["low_latency"],
        )
        for row in (name, quality, fps, low_latency, audio):
            group.add(row)
        page.add(group)

        group = Adw.PreferencesGroup(title="Access")
        pin = Adw.SwitchRow(
            title="Require a code",
            subtitle="New devices must enter a 4‑digit code shown in this window",
            active=cfg["pin"],
        )
        group.add(pin)
        page.add(group)

        group = Adw.PreferencesGroup(title="Troubleshooting")
        separate = Adw.SwitchRow(
            title="Use separate video window",
            subtitle="Try this if the picture stays black or looks broken",
            active=cfg["separate_window"],
        )
        group.add(separate)
        page.add(group)

        group = Adw.PreferencesGroup(title="Control")
        trackpad = Adw.SwitchRow(
            title="Trackpad mode",
            subtitle="Move the phone’s pointer like a laptop trackpad instead of jumping to where you click. "
            "Use this if clicks land in the wrong place or the pointer does not move",
            active=cfg["trackpad"],
        )
        group.add(trackpad)
        touch_scroll = Adw.SwitchRow(
            title="Touch‑style scrolling",
            subtitle="The wheel and two‑finger scrolling drag the page like a finger. "
            "Turn off to send mouse‑wheel steps instead",
            active=cfg["touch_scroll"],
        )
        group.add(touch_scroll)
        page.add(group)
        dialog.add(page)

        def apply(*_):
            new = dict(
                cfg,
                name=name.get_text().strip()[:40] or DEFAULTS["name"],
                resolution=RESOLUTIONS[quality.get_selected()][0],
                fps60=fps.get_active(),
                low_latency=low_latency.get_active(),
                audio=audio.get_active(),
                pin=pin.get_active(),
                separate_window=separate.get_active(),
                trackpad=trackpad.get_active(),
                touch_scroll=touch_scroll.get_active(),
            )
            if new == self.cfg:
                return
            restart = any(new[key] != self.cfg[key] for key in RECEIVER_KEYS)
            new_decoder = new["low_latency"] != self.cfg["low_latency"]
            self.cfg = new
            self._save_config()
            if self.remote:
                self.remote.release_all()
                self.remote.absolute = not new["trackpad"]
            if new_decoder:
                self.have_frame = False
                self._restart_pipeline()
            if restart:
                self.start_server()

        dialog.connect("closed", apply)
        dialog.present(self)

    def _act_log(self, *_):
        text = Gtk.TextView(
            editable=False, cursor_visible=False, monospace=True, css_classes=["log-view"],
            top_margin=12, bottom_margin=12, left_margin=12, right_margin=12,
        )
        text.get_buffer().set_text("\n".join(self.log_lines))
        scroller = Gtk.ScrolledWindow(child=text)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy Log")
        copy.connect("clicked", lambda *_: self.get_clipboard().set("\n".join(self.log_lines)))
        header = Adw.HeaderBar()
        header.pack_start(copy)
        view = Adw.ToolbarView(content=scroller)
        view.add_top_bar(header)
        dialog = Adw.Dialog(title="Receiver Log", content_width=760, content_height=520, child=view)
        dialog.present(self)

        def scroll_to_end():
            adj = scroller.get_vadjustment()
            adj.set_value(adj.get_upper())
            return GLib.SOURCE_REMOVE

        GLib.idle_add(scroll_to_end)

    def _act_about(self, *_):
        Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            version=VERSION,
            comments="Mirror an iPhone or iPad screen to this computer with AirPlay.\n\n"
            "The AirPlay receiver is provided by UxPlay.",
            website="https://github.com/FDH2/UxPlay",
            license_type=Gtk.License.GPL_3_0,
        ).present(self)

    def _on_close(self, *_):
        self.shutdown()
        return False

    def shutdown(self):
        self._cancel_idle_timer()
        for timer in ("_snap_timer", "_watchdog"):
            if getattr(self, timer):
                GLib.source_remove(getattr(self, timer))
                setattr(self, timer, 0)
        if self.remote:
            remote, self.remote = self.remote, None
            remote.stop()
        self.stop_server()
        self._drop_pipeline()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)

    def _on_signal(self):
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        if not self.window:
            self.window = Window(self)
        self.window.present()

    def do_shutdown(self):
        if self.window:
            self.window.shutdown()
        Adw.Application.do_shutdown(self)


def main():
    Gst.init(None)
    return App().run(sys.argv)
