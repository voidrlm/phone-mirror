"""Makes this computer look like a Bluetooth mouse and keyboard to the phone.

AirPlay only carries the picture, so control goes over a second link: a Bluetooth
Low Energy "HID over GATT" peripheral registered with BlueZ.  The phone pairs with
it like with any wireless mouse/keyboard.  On an iPhone the pointer only appears
while Settings > Accessibility > Touch > AssistiveTouch is on.
"""

import time

import dbus
import dbus.exceptions
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
SERVICE_IFACE = "org.bluez.GattService1"
CHRC_IFACE = "org.bluez.GattCharacteristic1"
DESC_IFACE = "org.bluez.GattDescriptor1"
ADV_IFACE = "org.bluez.LEAdvertisement1"
AGENT_IFACE = "org.bluez.Agent1"
BASE = "/io/github/voidrlm/PhoneMirror/hid"

# fmt: off
REPORT_MAP = bytes([
    # Report 1: keyboard (modifiers, reserved, 6 keys; LED output)
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0,
    # Report 2: relative mouse (3 buttons, X, Y, wheel)
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x05, 0x81, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,
    0xC0, 0xC0,
    # Report 3: absolute pointer (3 buttons, X and Y in 0..32767, wheel)
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x03, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x05, 0x81, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x15, 0x00, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x02,
    0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
    # Report 4: consumer control (one 16-bit usage: Home, Power, ...)
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x04,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
])

# Linux evdev key code -> USB HID keyboard usage
KEYS = {
    1: 0x29, 2: 0x1E, 3: 0x1F, 4: 0x20, 5: 0x21, 6: 0x22, 7: 0x23, 8: 0x24, 9: 0x25, 10: 0x26, 11: 0x27,
    12: 0x2D, 13: 0x2E, 14: 0x2A, 15: 0x2B,
    16: 0x14, 17: 0x1A, 18: 0x08, 19: 0x15, 20: 0x17, 21: 0x1C, 22: 0x18, 23: 0x0C, 24: 0x12, 25: 0x13,
    26: 0x2F, 27: 0x30, 28: 0x28,
    30: 0x04, 31: 0x16, 32: 0x07, 33: 0x09, 34: 0x0A, 35: 0x0B, 36: 0x0D, 37: 0x0E, 38: 0x0F,
    39: 0x33, 40: 0x34, 41: 0x35, 43: 0x31,
    44: 0x1D, 45: 0x1B, 46: 0x06, 47: 0x19, 48: 0x05, 49: 0x11, 50: 0x10, 51: 0x36, 52: 0x37, 53: 0x38,
    55: 0x55, 57: 0x2C, 58: 0x39,
    59: 0x3A, 60: 0x3B, 61: 0x3C, 62: 0x3D, 63: 0x3E, 64: 0x3F, 65: 0x40, 66: 0x41, 67: 0x42, 68: 0x43,
    69: 0x53, 70: 0x47, 71: 0x5F, 72: 0x60, 73: 0x61, 74: 0x56, 75: 0x5C, 76: 0x5D, 77: 0x5E, 78: 0x57,
    79: 0x59, 80: 0x5A, 81: 0x5B, 82: 0x62, 83: 0x63, 86: 0x64, 87: 0x44, 88: 0x45,
    96: 0x58, 98: 0x54, 99: 0x46,
    102: 0x4A, 103: 0x52, 104: 0x4B, 105: 0x50, 106: 0x4F, 107: 0x4D, 108: 0x51, 109: 0x4E, 110: 0x49, 111: 0x4C,
    127: 0x65,
}
# fmt: on

# evdev modifier key -> HID modifier bit.  Ctrl is sent as Command so that the
# usual Ctrl+C / Ctrl+V / Ctrl+A habits do the expected thing on iOS.
MODIFIERS = {29: 0x08, 97: 0x80, 42: 0x02, 54: 0x20, 56: 0x04, 100: 0x40, 125: 0x08, 126: 0x80}

BUTTON_LEFT, BUTTON_RIGHT, BUTTON_MIDDLE = 1, 2, 4

USAGE_HOME = 0x0223  # AC Home
USAGE_POWER = 0x0030  # locks the screen
USAGE_EJECT = 0x00B8  # toggles the on-screen keyboard on iOS


_bus = None


def _system_bus():
    """One connection for the life of the app.  (Closing a connection that is attached
    to the GLib main loop stalls the loop, so it is kept and reused.)"""
    global _bus
    if _bus is None:
        _bus = dbus.SystemBus(private=True, mainloop=DBusGMainLoop())
    return _bus


class Rejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class InvalidArgs(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


def _bytes(value):
    return dbus.Array([dbus.Byte(b) for b in value], signature="y")


class _Node(dbus.service.Object):
    """A D-Bus object exposing one BlueZ interface with read-only properties."""

    IFACE = ""

    def __init__(self, bus, path):
        self.path = path
        super().__init__(bus, path)

    def props(self):
        return {}

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != self.IFACE:
            raise InvalidArgs()
        return self.props()

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, name):
        if interface != self.IFACE or name not in self.props():
            raise InvalidArgs()
        return self.props()[name]

    @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class Service(_Node):
    IFACE = SERVICE_IFACE

    def __init__(self, bus, index, uuid):
        super().__init__(bus, f"{BASE}/service{index}")
        self.uuid = uuid
        self.chars = []

    def props(self):
        return {"UUID": self.uuid, "Primary": dbus.Boolean(True)}


class Characteristic(_Node):
    IFACE = CHRC_IFACE

    def __init__(self, bus, service, uuid, flags, value=b"", on_subscribe=None):
        super().__init__(bus, f"{service.path}/char{len(service.chars)}")
        self.service = service
        self.uuid = uuid
        self.flags = flags
        self.value = bytes(value)
        self.notifying = False
        self.descriptors = []
        self._on_subscribe = on_subscribe
        service.chars.append(self)

    def props(self):
        return {
            "Service": dbus.ObjectPath(self.service.path),
            "UUID": self.uuid,
            "Flags": dbus.Array(self.flags, signature="s"),
        }

    @dbus.service.method(CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return _bytes(self.value[int(options.get("offset", 0)):])

    @dbus.service.method(CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        self.value = bytes(bytearray(value))

    @dbus.service.method(CHRC_IFACE)
    def StartNotify(self):
        self._set_notifying(True)

    @dbus.service.method(CHRC_IFACE)
    def StopNotify(self):
        self._set_notifying(False)

    def _set_notifying(self, notifying):
        if notifying != self.notifying:
            self.notifying = notifying
            if self._on_subscribe:
                self._on_subscribe()

    def send(self, value):
        self.value = bytes(value)
        if self.notifying:
            self.PropertiesChanged(CHRC_IFACE, {"Value": _bytes(self.value)}, [])


class Descriptor(_Node):
    IFACE = DESC_IFACE

    def __init__(self, bus, characteristic, uuid, flags, value):
        super().__init__(bus, f"{characteristic.path}/desc{len(characteristic.descriptors)}")
        self.characteristic = characteristic
        self.uuid = uuid
        self.flags = flags
        self.value = bytes(value)
        characteristic.descriptors.append(self)

    def props(self):
        return {
            "Characteristic": dbus.ObjectPath(self.characteristic.path),
            "UUID": self.uuid,
            "Flags": dbus.Array(self.flags, signature="s"),
        }

    @dbus.service.method(DESC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return _bytes(self.value[int(options.get("offset", 0)):])


class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = BASE
        self.nodes = []
        super().__init__(bus, BASE)

    @dbus.service.method(OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return {dbus.ObjectPath(node.path): {node.IFACE: node.props()} for node in self.nodes}


class Advertisement(_Node):
    IFACE = ADV_IFACE

    def __init__(self, bus, name):
        super().__init__(bus, f"{BASE}/advertisement")
        self.name = name

    def props(self):
        return {
            "Type": "peripheral",
            "ServiceUUIDs": dbus.Array(["1812"], signature="s"),
            "Appearance": dbus.UInt16(0x03C0),  # generic human interface device
            "LocalName": self.name,
            "Discoverable": dbus.Boolean(True),
        }

    @dbus.service.method(ADV_IFACE)
    def Release(self):
        pass


class Agent(dbus.service.Object):
    """Asks the user before any device is allowed to pair while control is on."""

    def __init__(self, bus, ask):
        self.path = f"{BASE}/agent"
        self._bus = bus
        self._ask = ask
        super().__init__(bus, self.path)

    def _confirm(self, device, ok, err):
        name = str(device).rsplit("/", 1)[-1]
        try:
            props = dbus.Interface(self._bus.get_object(BLUEZ, device), PROPS_IFACE)
            name = str(props.Get(DEVICE_IFACE, "Alias"))
        except dbus.DBusException:
            pass
        self._ask(name, lambda allow: ok() if allow else err(Rejected("Pairing rejected")))

    @dbus.service.method(AGENT_IFACE, in_signature="o", async_callbacks=("ok", "err"))
    def RequestAuthorization(self, device, ok, err):
        self._confirm(device, ok, err)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", async_callbacks=("ok", "err"))
    def RequestConfirmation(self, device, passkey, ok, err):
        self._confirm(device, ok, err)

    @dbus.service.method(AGENT_IFACE, in_signature="os")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise Rejected("PIN pairing is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise Rejected("Passkey pairing is not supported")

    @dbus.service.method(AGENT_IFACE, in_signature="ouq")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_IFACE)
    def Cancel(self):
        pass

    @dbus.service.method(AGENT_IFACE)
    def Release(self):
        pass


class HidRemote:
    """state is one of: off, starting, waiting (for the phone), connected, error."""

    def __init__(self, name, on_state, ask_pairing, log):
        self.name = name
        self.on_state = on_state
        self.ask_pairing = ask_pairing
        self.log = log
        self.state = "off"
        self.error = ""
        self.absolute = True
        self._bus = None
        self._adapter_path = None
        self._was_pairable = True
        self._objects = []
        self._inputs = []
        self._pending = 0
        self._flush_id = 0
        self._swipe = None
        self._last_send = 0.0
        self.rel_scale = (480.0, 1000.0)  # mouse counts for one screen width / height (trackpad mode)
        self._reset_input()

    # ---- lifecycle -----------------------------------------------------------

    def start(self):
        if self.state not in ("off", "error"):
            return
        self.error = ""
        try:
            self._bus = _system_bus()
            manager = dbus.Interface(self._bus.get_object(BLUEZ, "/"), OM_IFACE)
            for path, interfaces in manager.GetManagedObjects().items():
                if "org.bluez.GattManager1" in interfaces and "org.bluez.LEAdvertisingManager1" in interfaces:
                    self._adapter_path = str(path)
                    break
            else:
                return self._fail("No Bluetooth adapter that can act as a mouse was found.")
            adapter = self._bus.get_object(BLUEZ, self._adapter_path)
            props = dbus.Interface(adapter, PROPS_IFACE)
            if not props.Get(ADAPTER_IFACE, "Powered"):
                return self._fail("Bluetooth is turned off.")
            self._was_pairable = bool(props.Get(ADAPTER_IFACE, "Pairable"))
            if not self._was_pairable:
                props.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(True))

            self._build()
            agents = dbus.Interface(self._bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
            agents.RegisterAgent(self._agent.path, "NoInputNoOutput")
            agents.RequestDefaultAgent(self._agent.path)

            self.state = "starting"
            self._pending = 2
            dbus.Interface(adapter, "org.bluez.GattManager1").RegisterApplication(
                self._app.path, {}, reply_handler=self._registered, error_handler=self._register_failed
            )
            dbus.Interface(adapter, "org.bluez.LEAdvertisingManager1").RegisterAdvertisement(
                self._advert.path, {}, reply_handler=self._registered, error_handler=self._register_failed
            )
        except dbus.DBusException as err:
            return self._fail(err.get_dbus_message() or str(err))
        self.on_state()

    def _build(self):
        bus = self._bus
        self._app = Application(bus)
        hid = Service(bus, 0, "1812")
        secure_read = ["encrypt-read"]
        Characteristic(bus, hid, "2a4a", secure_read, bytes([0x11, 0x01, 0x00, 0x02]))  # HID information
        Characteristic(bus, hid, "2a4b", secure_read, REPORT_MAP)
        Characteristic(bus, hid, "2a4c", ["write-without-response"])  # control point
        Characteristic(bus, hid, "2a4e", secure_read + ["write-without-response"], b"\x01")  # protocol mode

        def input_report(report_id, size):
            char = Characteristic(
                bus, hid, "2a4d", ["encrypt-read", "notify", "encrypt-notify"], bytes(size), self._subscription_changed
            )
            Descriptor(bus, char, "2908", secure_read, bytes([report_id, 1]))
            self._inputs.append(char)
            return char

        self._keyboard = input_report(1, 8)
        leds = Characteristic(bus, hid, "2a4d", ["encrypt-read", "encrypt-write", "write-without-response"], b"\x00")
        Descriptor(bus, leds, "2908", secure_read, bytes([1, 2]))
        self._mouse = input_report(2, 4)
        self._pointer = input_report(3, 6)
        self._consumer = input_report(4, 2)

        battery = Service(bus, 1, "180f")
        Characteristic(bus, battery, "2a19", ["read", "notify"], b"\x64")

        for service in (hid, battery):
            self._app.nodes.append(service)
            for char in service.chars:
                self._app.nodes.append(char)
                self._app.nodes.extend(char.descriptors)
        self._advert = Advertisement(bus, self.name)
        self._agent = Agent(bus, self.ask_pairing)
        self._objects = [self._app, self._advert, self._agent] + self._app.nodes

    def _registered(self):
        self._pending -= 1
        if self._pending == 0 and self.state == "starting":
            self.log("control: Bluetooth mouse/keyboard service is registered and advertising")
            self.state = "waiting"
            self._subscription_changed()

    def _register_failed(self, err):
        if self.state == "starting":
            self._fail(err.get_dbus_message() or str(err))

    def _fail(self, message):
        self.log(f"control: {message}")
        self.stop()
        self.state = "error"
        self.error = message
        self.on_state()

    def _subscription_changed(self):
        if self.state not in ("waiting", "connected"):
            return
        state = "connected" if any(char.notifying for char in self._inputs) else "waiting"
        if state != self.state:
            self.state = state
            self._reset_input()
            self.log(f"control: {'phone connected' if state == 'connected' else 'phone disconnected'}")
        self.on_state()

    def stop(self):
        if self.state == "connected":
            self.release_all()
        if self._flush_id:
            GLib.source_remove(self._flush_id)
            self._flush_id = 0
        bus, self._bus = self._bus, None
        if bus:
            try:
                if self._adapter_path and self._objects:
                    adapter = bus.get_object(BLUEZ, self._adapter_path)
                    for iface, method, path in (
                        ("org.bluez.LEAdvertisingManager1", "UnregisterAdvertisement", self._advert.path),
                        ("org.bluez.GattManager1", "UnregisterApplication", self._app.path),
                    ):
                        try:
                            getattr(dbus.Interface(adapter, iface), method)(path)
                        except dbus.DBusException:
                            pass
                    try:
                        dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1").UnregisterAgent(
                            self._agent.path
                        )
                    except dbus.DBusException:
                        pass
                    if not self._was_pairable:
                        dbus.Interface(adapter, PROPS_IFACE).Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(False))
            except dbus.DBusException:
                pass
            for obj in self._objects:
                obj.remove_from_connection()
        self._objects = []
        self._inputs = []
        self.state = "off"
        self.error = ""

    # ---- input ---------------------------------------------------------------

    def _reset_input(self):
        self._drop_swipe()
        self._buttons = 0
        self._x = self._y = 0.5
        self._dx = self._dy = self._wheel = 0.0
        self._modifiers = 0
        self._keys = []

    @property
    def connected(self):
        return self.state == "connected"

    def move_abs(self, x, y):
        """Pointer position as fractions (0..1) of the phone screen."""
        x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        if self._swipe:  # the "finger" is busy scrolling; go there once it lifts
            self._swipe["home"] = (x, y)
            return
        self._x, self._y = x, y
        self._schedule()

    def move_rel(self, dx, dy):
        if self._swipe:
            return
        self._dx += dx
        self._dy += dy
        self._schedule()

    # Touch-style scrolling: instead of mouse-wheel steps, put a finger down where the
    # pointer is, drag the page and lift again, exactly like scrolling on the phone.

    SWIPE_START = 0.02  # travel (fraction of the screen) before the finger goes down
    SWIPE_REACH = 0.45  # longest drag in trackpad mode before the finger is lifted

    def swipe(self, dx, dy, animate):
        """Drag the page by dx, dy (finger travel as fractions of the screen).

        animate=True is for mouse-wheel clicks: the jump is spread over a few frames
        and comes to rest, so the page moves a fixed amount.  animate=False follows a
        touchpad one-to-one; call swipe_end() when the fingers lift and the phone adds
        its own momentum.
        """
        if not self.connected:
            return
        swipe = self._swipe
        if swipe is None:
            swipe = self._swipe = {
                "down": False, "home": (self._x, self._y), "ax": self._x, "ay": self._y,
                "ox": 0.0, "oy": 0.0, "tx": 0.0, "ty": 0.0, "tick": 0, "idle": 0,
            }  # fmt: skip
        swipe["tx"] += dx
        swipe["ty"] += dy
        if swipe["idle"]:
            GLib.source_remove(swipe["idle"])
        swipe["idle"] = GLib.timeout_add(170 if animate else 400, self._swipe_idle)
        if not swipe["down"]:
            if max(abs(swipe["tx"]), abs(swipe["ty"])) < self.SWIPE_START:
                return  # too small to be a scroll; a tiny drag would count as a tap
            self._swipe_down()
        if animate:
            if not swipe["tick"]:
                swipe["tick"] = GLib.timeout_add(12, self._swipe_tick)
        else:
            self._swipe_to(swipe["tx"], swipe["ty"])

    def _swipe_down(self):
        swipe = self._swipe
        if self.absolute:
            # start away from the screen edges, where iOS has its own gestures (Home, back, ...)
            swipe["ax"] = min(max(self._x, 0.08), 0.92)
            swipe["ay"] = min(max(self._y, 0.12), 0.88)
            self._x, self._y = swipe["ax"], swipe["ay"]
        self._send_pointer()
        self._buttons |= BUTTON_LEFT
        self._send_pointer()
        swipe["down"] = True

    def _swipe_to(self, ox, oy, now=False):
        """Move the finger to offset ox, oy from where it went down."""
        swipe = self._swipe
        if self.absolute:
            x, y = swipe["ax"] + ox, swipe["ay"] + oy
            self._x, self._y = min(max(x, 0.02), 0.98), min(max(y, 0.03), 0.97)
            reached_edge = (self._x, self._y) != (x, y)
        else:
            self._dx += (ox - swipe["ox"]) * self.rel_scale[0]
            self._dy += (oy - swipe["oy"]) * self.rel_scale[1]
            reached_edge = max(abs(ox), abs(oy)) > self.SWIPE_REACH
        swipe["ox"], swipe["oy"] = ox, oy
        if reached_edge:
            self.swipe_end()  # out of room: lift; the next scroll event starts a new drag
        elif now:
            self._send_pointer()
        else:
            self._schedule()

    def _swipe_tick(self):
        swipe = self._swipe
        if not swipe or not swipe["down"]:
            return GLib.SOURCE_REMOVE
        rx, ry = swipe["tx"] - swipe["ox"], swipe["ty"] - swipe["oy"]
        if max(abs(rx), abs(ry)) < 0.003:
            swipe["tick"] = 0
            self._swipe_to(swipe["tx"], swipe["ty"], now=True)
            return GLib.SOURCE_REMOVE
        self._swipe_to(swipe["ox"] + rx * 0.25, swipe["oy"] + ry * 0.25, now=True)
        if self._swipe is not swipe:  # the drag ran out of room and ended
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _swipe_idle(self):
        if self._swipe:
            self._swipe["idle"] = 0
            self.swipe_end()
        return GLib.SOURCE_REMOVE

    def _drop_swipe(self):
        swipe, self._swipe = getattr(self, "_swipe", None), None
        if swipe:
            for timer in ("tick", "idle"):
                if swipe[timer]:
                    GLib.source_remove(swipe[timer])
        return swipe

    def swipe_end(self):
        """Lift the finger and put the pointer back where the mouse is."""
        swipe = self._drop_swipe()
        if not swipe or not swipe["down"] or not self.connected:
            return
        self._send_pointer()
        self._buttons &= ~BUTTON_LEFT
        self._send_pointer()
        if self.absolute:
            self._x, self._y = swipe["home"]
        else:
            self._dx -= swipe["ox"] * self.rel_scale[0]
            self._dy -= swipe["oy"] * self.rel_scale[1]
        self._schedule()

    def scroll(self, amount):
        self._wheel += amount
        self._schedule()

    def button(self, bit, pressed):
        if not self.connected:
            return
        self.swipe_end()
        self._send_pointer()  # get the pointer to the right place before the click lands
        self._buttons = (self._buttons | bit) if pressed else (self._buttons & ~bit)
        self._send_pointer()

    SEND_INTERVAL = 0.008  # never more than 125 pointer reports a second

    def _schedule(self):
        """Send the pointer state now, or as soon as the rate limit allows."""
        if not self.connected or self._flush_id:
            return
        wait = self.SEND_INTERVAL - (time.monotonic() - self._last_send)
        if wait <= 0:
            self._send_pointer()
        else:
            self._flush_id = GLib.timeout_add(max(1, round(wait * 1000)), self._flush)

    def _flush(self):
        self._flush_id = 0
        if self.connected:
            self._send_pointer()
        return GLib.SOURCE_REMOVE

    def _send_pointer(self):
        self._last_send = time.monotonic()
        wheel = max(-127, min(127, int(self._wheel)))
        self._wheel -= wheel
        if self.absolute:
            x, y = round(self._x * 32767), round(self._y * 32767)
            self._dx = self._dy = 0.0
            self._pointer.send(bytes([self._buttons, x & 0xFF, x >> 8, y & 0xFF, y >> 8, wheel & 0xFF]))
        else:
            dx = max(-127, min(127, int(self._dx)))
            dy = max(-127, min(127, int(self._dy)))
            self._dx -= dx
            self._dy -= dy
            self._mouse.send(bytes([self._buttons, dx & 0xFF, dy & 0xFF, wheel & 0xFF]))
            if abs(self._dx) >= 1 or abs(self._dy) >= 1:
                self._schedule()

    def key(self, keycode, pressed):
        """keycode is a Linux evdev key code.  Returns False for keys that cannot be sent."""
        if not self.connected:
            return False
        if keycode in MODIFIERS:
            bit = MODIFIERS[keycode]
            self._modifiers = (self._modifiers | bit) if pressed else (self._modifiers & ~bit)
        elif keycode in KEYS:
            usage = KEYS[keycode]
            if pressed and usage not in self._keys:
                self._keys = (self._keys + [usage])[-6:]
            elif not pressed and usage in self._keys:
                self._keys.remove(usage)
        else:
            return False
        self._send_keys()
        return True

    def _send_keys(self):
        self._keyboard.send(bytes([self._modifiers, 0] + self._keys + [0] * (6 - len(self._keys))))

    def release_all(self):
        """Let go of every key and button, e.g. when the window loses focus."""
        if not self.connected:
            return
        self.swipe_end()
        if self._modifiers or self._keys:
            self._modifiers, self._keys = 0, []
            self._send_keys()
        if self._buttons:
            self._buttons = 0
            self._send_pointer()

    def tap(self, usage, times=1):
        """Press and release a consumer-control key such as Home."""
        if not self.connected:
            return
        for index in range(times):
            GLib.timeout_add(index * 220, self._consumer_send, usage)
            GLib.timeout_add(index * 220 + 70, self._consumer_send, 0)

    def _consumer_send(self, usage):
        if self.connected:
            self._consumer.send(bytes([usage & 0xFF, usage >> 8]))
        return GLib.SOURCE_REMOVE
