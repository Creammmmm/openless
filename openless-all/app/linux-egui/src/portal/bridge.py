"""Private JSON-lines transport between the Linux host and desktop portals.

GIO owns the session bus, asynchronous authorization requests and Unix FD
transfers. No shell, input-method switching or network service is involved.
"""

import json
import os
import re
import select
import shlex
import sys
import threading
import time
import uuid
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

DEST = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
PREFIX = "org.freedesktop.portal."
MAX_MESSAGE = 4 * 1024 * 1024


def paste_keys(shortcut):
    return {
        "ctrlV": [0xFFE3, ord("v")],
        "ctrlShiftV": [0xFFE3, 0xFFE1, ord("v")],
        "shiftInsert": [0xFFE1, 0xFF63],
    }[shortcut]


def missing_portal(error):
    return bool(error) and any(code in error for code in (
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.portal.Error.NotFound",
    ))


class GnomeShortcuts:
    """GNOME 46 has custom shortcuts but no GlobalShortcuts portal backend.

    Only our two paths are added/removed. Keep their key choices for reconnects;
    never replace the user's custom-keybindings list or unrelated entries.
    """
    SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/openless-"

    def __init__(self, executable):
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
        source = Gio.SettingsSchemaSource.get_default()
        if "gnome" not in desktop or not source or not source.lookup(self.SCHEMA, True):
            raise RuntimeError("Global shortcuts are unavailable on this desktop")
        if not executable or not os.path.isfile(executable):
            raise RuntimeError("OpenLess executable is unavailable for GNOME shortcuts")
        self.settings = Gio.Settings.new(self.SCHEMA)
        self.entries = []
        for action, binding in (("toggle-dictation", "<Control><Alt>space"),
                                ("cancel-dictation", "<Control><Alt>Escape")):
            path = self.PREFIX + action + "/"
            entry = Gio.Settings.new_with_path(self.SCHEMA + ".custom-keybinding", path)
            command = shlex.quote(executable) + " --portal-hotkey --" + action
            self.entries.append((path, entry, command, binding))

    @staticmethod
    def normalized(binding):
        aliases = {"primary": "control", "ctrl": "control", "mod1": "alt"}
        modifiers = [aliases.get(value.lower(), value.lower())
                     for value in re.findall(r"<([^>]+)>", binding)]
        key = re.sub(r"<[^>]+>", "", binding).lower()
        return tuple(sorted(modifiers)), key

    def used_bindings(self, paths):
        source = Gio.SettingsSchemaSource.get_default()
        used = set()
        owned = {entry[0] for entry in self.entries}
        for path in paths:
            if path not in owned and path.startswith("/") and path.endswith("/"):
                entry = Gio.Settings.new_with_path(self.SCHEMA + ".custom-keybinding", path)
                used.add(self.normalized(entry.get_string("binding")))
        for name in (self.SCHEMA, "org.gnome.desktop.wm.keybindings",
                     "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings",
                     "org.gnome.mutter.wayland.keybindings"):
            schema = source.lookup(name, True)
            if schema:
                settings = Gio.Settings.new(name)
                for key in schema.list_keys():
                    value = settings.get_value(key)
                    if value.get_type_string() == "as" and key != "custom-keybindings":
                        used.update(self.normalized(binding) for binding in value.unpack())
        return used

    def needs_bindings(self):
        return any(not entry.get_string("binding") for _, entry, _, _ in self.entries)

    def enable(self):
        paths = list(self.settings.get_strv("custom-keybindings"))
        if not self.settings.is_writable("custom-keybindings"):
            raise RuntimeError("GNOME custom shortcuts are locked by system policy")
        used = self.used_bindings(paths)
        # Check every entry before changing anything.
        for path, entry, command, _binding in self.entries:
            old = entry.get_string("command")
            if old and old != command and " --portal-hotkey --" not in old:
                raise RuntimeError("An OpenLess shortcut path is occupied; check GNOME Keyboard settings")
            if not all(entry.is_writable(key) for key in ("name", "command", "binding")):
                raise RuntimeError("GNOME custom shortcut is locked by system policy")
        for path, entry, command, binding in self.entries:
            entry.set_string("name", "OpenLess: " + path.rsplit("/", 2)[-2].removeprefix("openless-"))
            entry.set_string("command", command)
            if entry.get_user_value("binding") is None:
                entry.set_string("binding", "" if self.normalized(binding) in used else binding)
            if path not in paths:
                paths.append(path)
        if not self.settings.set_strv("custom-keybindings", paths):
            raise RuntimeError("Unable to enable GNOME custom shortcuts")
        Gio.Settings.sync()

    def disable(self):
        paths = list(self.settings.get_strv("custom-keybindings"))
        owned = {path for path, entry, command, _ in self.entries
                 if entry.get_string("command") == command}
        remaining = [path for path in paths if path not in owned]
        if remaining != paths:
            self.settings.set_strv("custom-keybindings", remaining)
            Gio.Settings.sync()

    @staticmethod
    def configure():
        Gio.Subprocess.new(["gnome-control-center", "keyboard"], Gio.SubprocessFlags.NONE)


class Bridge:
    def __init__(self, state_path=None, executable=None):
        # GTK and other GIO users may already have called a portal on the shared
        # bus connection. Registry.Register must be its peer's first portal call.
        self.bus = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        self.executable = executable
        self.legacy_shortcuts = None
        self.legacy_active = False
        self.loop = GLib.MainLoop()
        self.requests = {}
        self.sessions = {}
        self.clipboard = b""
        self.clipboard_owner = False
        self.held = set()
        self.cancelled = set()
        self.buffer = b""
        self.connecting = False
        self.registered = False
        self.registering = False
        self.portal_epoch = 0
        self.generation = 0
        self.write_lock = threading.Lock()
        self.state_path = Path(state_path) if state_path else None
        self.saved = {}
        if self.state_path:
            try:
                if self.state_path.stat().st_size <= 16384:
                    value = json.loads(self.state_path.read_text())
                    if isinstance(value, dict):
                        self.saved = value
            except (OSError, ValueError):
                pass
        self.bus.signal_subscribe(DEST, None, None, None, None,
                                  Gio.DBusSignalFlags.NONE, self.signal)
        self.bus.connect("closed", lambda *_: self.disconnect("Desktop connection closed"))
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus",
                                  "NameOwnerChanged", "/org/freedesktop/DBus", DEST,
                                  Gio.DBusSignalFlags.NONE,
                                  self.owner_changed)
        GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, self.read)

    def save(self):
        if not self.state_path:
            return
        temporary = self.state_path.with_name(self.state_path.name + "." + uuid.uuid4().hex)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(self.saved, stream)
            os.replace(temporary, self.state_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            self.status("Desktop permissions could not be saved; reconnect after restart")

    def emit(self, **message):
        with self.write_lock:
            print(json.dumps(message, ensure_ascii=False), flush=True)

    def status(self, message=""):
        if not message and self.legacy_active and self.legacy_shortcuts.needs_bindings():
            message = "Assign OpenLess shortcuts in GNOME Keyboard settings; existing shortcuts were preserved."
        self.emit(event="status", shortcuts="shortcuts" in self.sessions or self.legacy_active,
                  input="input" in self.sessions, connecting=self.connecting,
                  gnome_shortcuts=self.legacy_shortcuts is not None,
                  message=message)

    def reply(self, request, error=None, **data):
        if request.get("id") is not None:
            self.emit(id=request["id"], error=error, **data)

    def call(self, interface, method, signature, args, callback=None):
        def done(bus, result):
            try:
                value = bus.call_finish(result).unpack()
                if callback:
                    callback(value, None)
            except GLib.Error as error:
                if callback:
                    callback(None, str(error))
                else:
                    self.status(str(error))
        self.bus.call(DEST, PATH, PREFIX + interface, method,
                      GLib.Variant(signature, args), None,
                      Gio.DBusCallFlags.NONE, 10000, None, done)

    def request(self, interface, method, signature, args, options, callback):
        token = "openless_" + uuid.uuid4().hex
        options["handle_token"] = GLib.Variant("s", token)
        owner = self.bus.get_unique_name()[1:].replace(".", "_")
        path = "/org/freedesktop/portal/desktop/request/" + owner + "/" + token
        generation = self.generation

        def complete(result, error):
            if generation == self.generation:
                callback(result, error)

        self.requests[path] = complete
        request_path = [path]

        def expired():
            handler = self.requests.pop(request_path[0], None)
            if handler:
                self.bus.call(DEST, request_path[0], PREFIX + "Request", "Close", None,
                              None, Gio.DBusCallFlags.NONE, 2000, None, None)
                handler(None, "Desktop authorization timed out. Connect again in Settings.")
            return False
        GLib.timeout_add_seconds(120, expired)

        def sent(value, error):
            if error:
                handler = self.requests.pop(path, None)
                if handler:
                    handler(None, error)
            elif value and value[0] != path:
                handler = self.requests.pop(path, None)
                if handler:
                    request_path[0] = value[0]
                    self.requests[value[0]] = handler

        self.call(interface, method, signature, args, sent)

    def close_sessions(self):
        if self.legacy_shortcuts:
            self.legacy_shortcuts.disable()
        self.legacy_active = False
        self.generation += 1
        for path in self.requests:
            self.bus.call(DEST, path, PREFIX + "Request", "Close", None,
                          None, Gio.DBusCallFlags.NONE, 2000, None, None)
        self.requests.clear()
        for session in self.sessions.values():
            self.bus.call(DEST, session, PREFIX + "Session", "Close", None,
                          None, Gio.DBusCallFlags.NONE, 2000, None, None)
        self.sessions.clear()
        self.held.clear()
        self.clipboard_owner = False

    def disconnect(self, message):
        self.close_sessions()
        self.connecting = False
        self.status(message)

    def connect(self):
        if self.connecting:
            return
        self.close_sessions()
        self.legacy_shortcuts = None
        self.connecting = True
        self.status()
        if self.registered:
            self.create_shortcuts()
            return
        if self.registering:
            return
        self.registering = True
        epoch = self.portal_epoch
        # Host registration gives non-Flatpak launches a stable desktop identity.
        def registered(_value, error):
            # Registry was introduced after Ubuntu 24.04. Older portals
            # identify native clients themselves; only missing APIs allow fallback.
            if missing_portal(error):
                error = None
            if epoch != self.portal_epoch:
                return
            self.registering = False
            # Registration belongs to the bus peer, even if this connection
            # attempt was cancelled while the reply was in flight.
            if not error:
                self.registered = True
            if self.connecting:
                if error:
                    self.connecting = False
                    hint = ("Install the OpenLess desktop entry before connecting. "
                            if "App info not found" in error else "")
                    self.status("Desktop identity registration failed. " + hint + error)
                else:
                    self.create_shortcuts()
        self.bus.call(DEST, PATH, "org.freedesktop.host.portal.Registry", "Register",
                      GLib.Variant("(sa{sv})", ("top.openless.OpenLess", {})), None,
                      Gio.DBusCallFlags.NONE, 5000, None,
                      lambda bus, result: self.registration_done(bus, result, registered))

    @staticmethod
    def registration_done(bus, result, callback):
        try:
            callback(bus.call_finish(result), None)
        except GLib.Error as error:
            callback(None, str(error))

    def owner_changed(self, *_args):
        # Initial D-Bus activation is not a restart; the queued Register call
        # still belongs to the new service and must be allowed to complete.
        if not _args[-1].unpack()[1]:
            return
        self.portal_epoch += 1
        self.registered = False
        self.registering = False
        self.disconnect("Desktop service restarted. Connect again in Settings.")

    def create_legacy_shortcuts(self):
        try:
            self.legacy_shortcuts = GnomeShortcuts(self.executable)
        except (RuntimeError, GLib.Error) as error:
            self.connecting = False
            self.status(str(error))
            return
        self.create_input()

    def create_shortcuts(self):
        options = {"session_handle_token": GLib.Variant("s", "ol_" + uuid.uuid4().hex)}
        def created(result, error):
            if missing_portal(error):
                self.create_legacy_shortcuts()
                return
            if error:
                self.connecting = False
                self.status(error)
                return
            session = result["session_handle"]
            self.sessions["pending_shortcuts"] = session
            bindings = [
                ("dictation", {"description": GLib.Variant("s", "OpenLess: start/stop dictation"),
                               "preferred_trigger": GLib.Variant("s", "CTRL+ALT+space")}),
                ("cancel", {"description": GLib.Variant("s", "OpenLess: cancel dictation"),
                            "preferred_trigger": GLib.Variant("s", "CTRL+ALT+Escape")}),
            ]
            options = {}
            def bound(_result, error):
                if error:
                    self.disconnect(error)
                else:
                    self.sessions.pop("pending_shortcuts", None)
                    self.sessions["shortcuts"] = session
                    self.create_input()
            self.request("GlobalShortcuts", "BindShortcuts", "(oa(sa{sv})sa{sv})",
                         (session, bindings, "", options), options, bound)
        self.request("GlobalShortcuts", "CreateSession", "(a{sv})", (options,), options, created)

    def create_input(self):
        generation = self.generation
        options = {"session_handle_token": GLib.Variant("s", "ol_" + uuid.uuid4().hex)}
        def created(result, error):
            if error:
                self.connecting = False
                self.status(error)
                return
            session = result["session_handle"]
            # Do not advertise input until keyboard and clipboard were granted.
            self.sessions["pending_input"] = session
            options = {"types": GLib.Variant("u", 1), "persist_mode": GLib.Variant("u", 2)}
            token = self.saved.pop("restore_token", None)
            if isinstance(token, str) and token:
                options["restore_token"] = GLib.Variant("s", token)
                self.save()
            def selected(_result, error):
                if error:
                    self.input_failed(error)
                    return
                self.call("Clipboard", "RequestClipboard", "(oa{sv})", (session, {}), clipboard)
            def clipboard(_result, error):
                if generation != self.generation:
                    return
                if error:
                    self.input_failed(error)
                    return
                options = {}
                self.request("RemoteDesktop", "Start", "(osa{sv})",
                             (session, "", options), options, started)
            def started(result, error):
                if error or not result.get("devices", 0) & 1 or not result.get("clipboard_enabled"):
                    self.input_failed(error or "Keyboard and clipboard access are required")
                    return
                if self.legacy_shortcuts:
                    try:
                        self.legacy_shortcuts.enable()
                    except (RuntimeError, GLib.Error) as error:
                        self.input_failed(str(error))
                        return
                    self.legacy_active = True
                self.sessions.pop("pending_input", None)
                self.sessions["input"] = session
                self.saved["enabled"] = True
                token = result.get("restore_token")
                if token:
                    self.saved["restore_token"] = token
                self.save()
                self.connecting = False
                self.status()
            self.request("RemoteDesktop", "SelectDevices", "(oa{sv})",
                         (session, options), options, selected)
        self.request("RemoteDesktop", "CreateSession", "(a{sv})", (options,), options, created)

    def input_failed(self, error):
        self.saved["enabled"] = False
        self.save()
        session = self.sessions.pop("pending_input", None)
        if session:
            self.bus.call(DEST, session, PREFIX + "Session", "Close", None,
                          None, Gio.DBusCallFlags.NONE, 2000, None, None)
        self.connecting = False
        self.status(error)

    def signal(self, _bus, _sender, path, interface, name, params):
        args = params.unpack()
        if interface == PREFIX + "Request" and name == "Response":
            callback = self.requests.pop(path, None)
            if callback:
                callback(args[1], None if args[0] == 0 else "Desktop permission was cancelled or denied")
        elif interface == PREFIX + "Session" and name == "Closed":
            if path in self.sessions.values():
                self.disconnect("Desktop permission ended. Connect again in Settings.")
        elif interface == PREFIX + "GlobalShortcuts" and args and args[0] == self.sessions.get("shortcuts"):
            if name == "Activated" and args[1] not in self.held:
                self.held.add(args[1])
                self.emit(event="hotkey", action=args[1])
            elif name == "Deactivated":
                self.held.discard(args[1])
        elif interface == PREFIX + "Clipboard" and args and args[0] == self.sessions.get("input"):
            if name == "SelectionOwnerChanged":
                self.clipboard_owner = args[1].get("session_is_owner", False)
            elif name == "SelectionTransfer":
                self.transfer(args[0], args[1], args[2])

    def transfer(self, session, mime, serial):
        if mime not in ("text/plain;charset=utf-8", "text/plain"):
            self.call("Clipboard", "SelectionWriteDone", "(oub)", (session, serial, False))
            return
        payload = self.clipboard
        def ready(bus, result):
            try:
                value, fds = bus.call_with_unix_fd_list_finish(result)
                fd = fds.get(value.unpack()[0])
            except GLib.Error:
                self.call("Clipboard", "SelectionWriteDone", "(oub)", (session, serial, False))
                return
            def write():
                success = False
                try:
                    os.set_blocking(fd, False)
                    offset = 0
                    deadline = time.monotonic() + 5
                    while offset < len(payload) and time.monotonic() < deadline:
                        if select.select([], [fd], [], 0.1)[1]:
                            offset += os.write(fd, payload[offset:])
                    success = offset == len(payload)
                except OSError:
                    pass
                finally:
                    os.close(fd)
                GLib.idle_add(lambda: self.call("Clipboard", "SelectionWriteDone", "(oub)",
                                                (session, serial, success)))
            threading.Thread(target=write, daemon=True).start()
        self.bus.call_with_unix_fd_list(DEST, PATH, PREFIX + "Clipboard", "SelectionWrite",
                                       GLib.Variant("(ou)", (session, serial)), None,
                                       Gio.DBusCallFlags.NONE, 5000, None, None, ready)

    def copy(self, command):
        if command.get("session") in self.cancelled:
            self.reply(command, "Insertion cancelled")
            return
        session = self.sessions.get("input")
        if not session:
            self.reply(command, "Connect desktop input in Settings first")
            return
        text = command.get("text", "")
        self.clipboard = text.encode("utf-8")
        self.clipboard_owner = False
        options = {"mime_types": GLib.Variant("as", ["text/plain;charset=utf-8", "text/plain"])}
        generation = self.generation
        def selected(_result, error):
            if generation != self.generation or command.get("session") in self.cancelled:
                self.reply(command, "Insertion cancelled")
            elif error:
                self.reply(command, error)
            elif command["op"] == "copy":
                self.reply(command, outcome="copiedFallback")
            else:
                deadline = time.monotonic() + 2
                def paste():
                    if generation != self.generation or command.get("session") in self.cancelled:
                        self.reply(command, "Insertion cancelled")
                        return False
                    if self.held or not self.clipboard_owner:
                        if time.monotonic() < deadline:
                            return True
                        self.reply(command, outcome="copiedFallback")
                        return False
                    self.send_paste(command, session)
                    return False
                GLib.timeout_add(20, paste)
        self.call("Clipboard", "SetSelection", "(oa{sv})", (session, options), selected)

    def send_paste(self, command, session):
        keys = paste_keys(command.get("shortcut", "ctrlV"))
        pressed = []
        failure = None
        try:
            for key in keys:
                # Include a key in cleanup even if the reply to its press is lost.
                pressed.append(key)
                self.bus.call_sync(DEST, PATH, PREFIX + "RemoteDesktop", "NotifyKeyboardKeysym",
                                   GLib.Variant("(oa{sv}iu)", (session, {}, key, 1)), None,
                                   Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error as error:
            failure = str(error)
        finally:
            for key in reversed(pressed):
                try:
                    self.bus.call_sync(DEST, PATH, PREFIX + "RemoteDesktop", "NotifyKeyboardKeysym",
                                       GLib.Variant("(oa{sv}iu)", (session, {}, key, 0)), None,
                                       Gio.DBusCallFlags.NONE, 1000, None)
                except GLib.Error as error:
                    failure = str(error)
        # Never retry a paste whose delivery is uncertain.
        self.reply(command, outcome="pasteSent" if failure is None else "copiedFallback")

    def command(self, command):
        op = command.get("op")
        if op == "connect":
            self.connect()
        elif op == "configure":
            session = self.sessions.get("shortcuts")
            def configured(_value, error):
                if missing_portal(error):
                    try:
                        GnomeShortcuts.configure()
                    except GLib.Error as failure:
                        self.status(str(failure))
                elif error:
                    self.status(error)
            if self.legacy_active:
                configured(None, "org.freedesktop.DBus.Error.UnknownMethod")
            elif session:
                self.call("GlobalShortcuts", "ConfigureShortcuts", "(osa{sv})", (session, "", {}), configured)
        elif op in ("copy", "insert"):
            self.copy(command)
        elif op == "cancel":
            self.cancelled.add(command["session"])
        elif op == "shutdown":
            self.close_sessions()
            self.loop.quit()
        elif op == "disconnect":
            self.saved = {"enabled": False}
            self.save()
            self.disconnect("")
        else:
            self.reply(command, "Unknown desktop command")

    def read(self, _fd, condition):
        if condition & GLib.IO_HUP:
            self.close_sessions()
            self.loop.quit()
            return False
        data = os.read(sys.stdin.fileno(), 65536)
        if not data:
            self.loop.quit()
            return False
        self.buffer += data
        if len(self.buffer) > MAX_MESSAGE:
            self.loop.quit()
            return False
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                self.command(json.loads(line))
            except (ValueError, KeyError, TypeError) as error:
                self.emit(event="error", message=str(error))
        return True

    def run(self):
        self.status()
        if self.saved.get("enabled"):
            self.connect()
        self.loop.run()


if __name__ == "__main__":
    Bridge(sys.argv[1] if len(sys.argv) > 1 else None,
           sys.argv[2] if len(sys.argv) > 2 else None).run()
