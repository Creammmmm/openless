"""Portal lifecycle tests; no desktop session or permission dialogs required."""

import importlib.util
import os

# These tests must never change the developer's desktop keybindings.
os.environ["GSETTINGS_BACKEND"] = "memory"
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "bridge", Path(__file__).parents[1] / "src/portal/bridge.py"
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def fixture():
    instance = object.__new__(bridge.Bridge)
    instance.legacy_shortcuts = None
    instance.legacy_active = False
    instance.executable = "/usr/bin/openless"
    instance.bus = Mock()
    instance.emit = Mock()
    instance.requests = {}
    instance.sessions = {"shortcuts": "/shortcuts", "input": "/input"}
    instance.held = set()
    instance.cancelled = set()
    instance.generation = 1
    instance.clipboard = b""
    instance.clipboard_owner = False
    instance.connecting = False
    instance.registered = False
    instance.registering = False
    instance.portal_epoch = 0
    instance.saved = {}
    instance.state_path = None
    return instance


class PortalTests(unittest.TestCase):
    def test_bridge_uses_a_private_bus_connection(self):
        bus = Mock()
        with patch.object(bridge.Gio, "bus_get_sync") as shared, \
             patch.object(bridge.Gio, "dbus_address_get_for_bus_sync", return_value="unix:path=/test"), \
             patch.object(bridge.Gio.DBusConnection, "new_for_address_sync", return_value=bus) as private, \
             patch.object(bridge.GLib, "io_add_watch"), \
             patch.object(bridge.sys, "stdin"):
            instance = bridge.Bridge()
        self.assertIs(instance.bus, bus)
        shared.assert_not_called()
        private.assert_called_once_with(
            "unix:path=/test",
            bridge.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | bridge.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )

    def test_reconnecting_does_not_register_the_same_bus_peer_twice(self):
        p = fixture()
        p.registered = True
        p.create_shortcuts = Mock()
        p.connect()
        p.create_shortcuts.assert_called_once()
        self.assertFalse(any(call.args[3] == "Register" for call in p.bus.call.call_args_list))

    def test_missing_desktop_identity_stops_before_permission_requests(self):
        p = fixture()
        p.create_shortcuts = Mock()
        p.bus.call_finish.side_effect = bridge.GLib.Error("App info not found")
        p.connect()
        callback = p.bus.call.call_args.args[-1]
        callback(p.bus, None)
        p.create_shortcuts.assert_not_called()
        self.assertFalse(p.connecting)
        self.assertIn("desktop entry", p.emit.call_args.kwargs["message"])

    def test_registration_reply_after_disconnect_is_reused(self):
        p = fixture()
        p.create_shortcuts = Mock()
        p.connect()
        callback = p.bus.call.call_args.args[-1]
        p.disconnect("cancelled")
        callback(p.bus, None)
        self.assertTrue(p.registered)
        p.create_shortcuts.assert_not_called()
        p.connect()
        p.create_shortcuts.assert_called_once()

    def test_reconnect_during_registration_reuses_pending_call(self):
        p = fixture()
        p.create_shortcuts = Mock()
        p.connect()
        callback = p.bus.call.call_args.args[-1]
        p.disconnect("cancelled")
        p.connect()
        registrations = [c for c in p.bus.call.call_args_list if c.args[3] == "Register"]
        self.assertEqual(len(registrations), 1)
        callback(p.bus, None)
        p.create_shortcuts.assert_called_once()

    def test_old_service_registration_reply_does_not_register_new_service(self):
        p = fixture()
        p.create_shortcuts = Mock()
        p.connect()
        callback = p.bus.call.call_args.args[-1]
        p.owner_changed(None, bridge.GLib.Variant("(sss)", (bridge.DEST, ":1.1", "")))
        callback(p.bus, None)
        self.assertFalse(p.registered)
        p.create_shortcuts.assert_not_called()

    def test_initial_portal_activation_does_not_cancel_registration(self):
        p = fixture()
        p.create_shortcuts = Mock()
        p.connect()
        callback = p.bus.call.call_args.args[-1]
        p.owner_changed(None, bridge.GLib.Variant("(sss)", (bridge.DEST, "", ":1.1")))
        callback(p.bus, None)
        p.create_shortcuts.assert_called_once()

    def test_clipboard_permission_reply_after_disconnect_does_not_start_session(self):
        p = fixture()
        p.request = Mock()
        p.call = Mock()
        p.create_input()
        p.request.call_args.args[-1]({"session_handle": "/pending"}, None)
        p.request.call_args.args[-1]({}, None)
        callback = p.call.call_args.args[-1]
        p.disconnect("cancelled")
        calls = p.request.call_count
        callback(None, None)
        self.assertEqual(p.request.call_count, calls)
        self.assertNotIn("input", p.sessions)

    def test_redirected_request_still_times_out_and_closes(self):
        p = fixture()
        p.bus.get_unique_name.return_value = ":1.9"
        p.call = Mock()
        callback = Mock()
        with patch.object(bridge.GLib, "timeout_add_seconds") as timer:
            p.request("RemoteDesktop", "CreateSession", "(a{sv})", ({},), {}, callback)
            p.call.call_args.args[-1](("/alternate",), None)
            timer.call_args.args[-1]()
        self.assertFalse(p.requests)
        self.assertIn("timed out", callback.call_args.args[1])
        self.assertEqual(p.bus.call.call_args.args[1], "/alternate")

    def test_old_portal_without_registry_continues_to_shortcuts(self):
        p = fixture()
        p.create_shortcuts = Mock()
        p.bus.call_finish.side_effect = bridge.GLib.Error(
            "GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod: no Registry"
        )
        p.connect()
        p.bus.call.call_args.args[-1](p.bus, None)
        self.assertTrue(p.registered)
        p.create_shortcuts.assert_called_once()

    def test_missing_global_shortcuts_uses_gnome_but_denial_does_not(self):
        for error, expected in (("org.freedesktop.DBus.Error.UnknownMethod", 1),
                                ("Desktop permission was cancelled or denied", 0)):
            p = fixture()
            p.request = Mock()
            p.create_legacy_shortcuts = Mock()
            with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
                p.create_shortcuts()
            p.request.call_args.args[-1](None, error)
            self.assertEqual(p.create_legacy_shortcuts.call_count, expected)

    def test_gnome_shortcuts_are_not_enabled_before_input_authorization(self):
        p = fixture()
        p.create_input = Mock()
        with patch.object(bridge, "GnomeShortcuts") as shortcuts:
            p.create_legacy_shortcuts()
            shortcuts.return_value.enable.assert_not_called()
        p.create_input.assert_called_once()

    def test_legacy_keys_activate_only_after_keyboard_and_clipboard_are_granted(self):
        for result, granted in (({"devices": 1, "clipboard_enabled": True}, True),
                                ({"devices": 1, "clipboard_enabled": False}, False)):
            p = fixture()
            p.sessions = {}
            p.request = Mock()
            p.call = Mock()
            p.legacy_shortcuts = Mock()
            p.create_input()
            p.request.call_args.args[-1]({"session_handle": "/legacy_input"}, None)
            p.request.call_args.args[-1]({}, None)
            p.call.call_args.args[-1](None, None)
            p.legacy_shortcuts.enable.assert_not_called()
            p.request.call_args.args[-1](result, None)
            self.assertEqual(p.legacy_shortcuts.enable.call_count, int(granted))
            self.assertEqual(p.legacy_active, granted)
            self.assertEqual("input" in p.sessions, granted)

    def test_disconnect_removes_only_our_legacy_shortcuts(self):
        p = fixture()
        p.legacy_shortcuts = Mock()
        p.legacy_active = True
        p.disconnect("")
        p.legacy_shortcuts.disable.assert_called_once()
        self.assertFalse(p.legacy_active)

    def test_missing_configure_method_opens_gnome_keyboard_settings(self):
        p = fixture()
        p.call = Mock()
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), \
             patch.object(bridge.GnomeShortcuts, "configure") as configure:
            p.command({"op": "configure"})
            p.call.call_args.args[-1](None, "org.freedesktop.DBus.Error.UnknownMethod")
            configure.assert_called_once()

    def test_gnome_versions_use_custom_shortcuts_without_portal_requests(self):
        for desktop in ("ubuntu:GNOME", "GNOME", "ubuntu"):
            p = fixture()
            p.request = Mock()
            p.create_legacy_shortcuts = Mock()
            with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": desktop}):
                p.create_shortcuts()
            p.request.assert_not_called()
            p.create_legacy_shortcuts.assert_called_once()

    def test_gnome_configure_bypasses_missing_portal_method_and_clears_error(self):
        for connected in (True, False):
            p = fixture()
            p.call = Mock()
            if not connected:
                p.sessions = {}
            with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}), \
                 patch.object(bridge.GnomeShortcuts, "configure") as configure:
                p.command({"op": "configure"})
            configure.assert_called_once()
            p.call.assert_not_called()
            self.assertEqual(p.emit.call_args.kwargs["message"], "")

    def test_gnome_configure_reports_failure_to_open_settings(self):
        p = fixture()
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), \
             patch.object(bridge.GnomeShortcuts, "configure", side_effect=bridge.GLib.Error("missing settings")):
            p.command({"op": "configure"})
        self.assertIn("missing settings", p.emit.call_args.kwargs["message"])

    def test_global_shortcut_repeat_is_one_toggle_until_release(self):
        p = fixture()
        args = bridge.GLib.Variant("(osta{sv})", ("/shortcuts", "dictation", 1, {}))
        for name in ["Activated", "Activated", "Deactivated", "Activated"]:
            p.signal(None, None, "/desktop", bridge.PREFIX + "GlobalShortcuts", name, args)
        self.assertEqual(p.emit.call_count, 2)
        p.emit.assert_called_with(event="hotkey", action="dictation")

    def test_events_from_a_previous_session_are_ignored(self):
        p = fixture()
        args = bridge.GLib.Variant("(osta{sv})", ("/old", "dictation", 1, {}))
        p.signal(None, None, "/desktop", bridge.PREFIX + "GlobalShortcuts", "Activated", args)
        p.emit.assert_not_called()

    def test_cancelled_insert_does_not_replace_clipboard(self):
        p = fixture()
        p.cancelled.add("recording1")
        p.copy({"op": "insert", "id": 7, "session": "recording1", "text": "秘密"})
        p.bus.call.assert_not_called()
        self.assertEqual(p.clipboard, b"")
        self.assertIn("cancelled", p.emit.call_args.kwargs["error"])

    def test_missing_permission_preserves_result_for_host_history(self):
        p = fixture()
        p.sessions.pop("input")
        p.copy({"op": "insert", "id": 7, "text": "中文"})
        self.assertIn("Settings", p.emit.call_args.kwargs["error"])
        p.bus.call.assert_not_called()

    def test_lost_press_reply_still_releases_every_attempted_key(self):
        p = fixture()
        p.bus.call_sync.side_effect = [None, bridge.GLib.Error("lost reply"), None, None]
        p.send_paste({"id": 7, "shortcut": "ctrlV"}, "/input")
        events = [call.args[4].unpack()[2:] for call in p.bus.call_sync.call_args_list]
        self.assertEqual(events, [(0xFFE3, 1), (ord("v"), 1), (ord("v"), 0), (0xFFE3, 0)])
        p.emit.assert_called_with(id=7, error=None, outcome="copiedFallback")

    def test_terminal_paste_never_sends_return(self):
        p = fixture()
        p.send_paste({"id": 7, "shortcut": "ctrlShiftV"}, "/input")
        events = [call.args[4].unpack()[2:] for call in p.bus.call_sync.call_args_list]
        self.assertEqual(events, [(0xFFE3, 1), (0xFFE1, 1), (ord("v"), 1),
                                  (ord("v"), 0), (0xFFE1, 0), (0xFFE3, 0)])
        p.emit.assert_called_with(id=7, error=None, outcome="pasteSent")

    def test_disconnect_invalidates_pending_paste(self):
        p = fixture()
        p.call = Mock()
        p.send_paste = Mock()
        with patch.object(bridge.GLib, "timeout_add") as timer:
            p.copy({"op": "insert", "id": 7, "session": "s", "text": "中文🌍"})
            p.call.call_args.args[4](None, None)  # SetSelection response
            pending = timer.call_args.args[1]
            p.disconnect("permission revoked")
            self.assertFalse(pending())
        p.send_paste.assert_not_called()
        self.assertIn("cancelled", p.emit.call_args.kwargs["error"])

    def test_paste_waits_for_shortcut_release_and_clipboard_ownership(self):
        p = fixture()
        p.call = Mock()
        p.send_paste = Mock()
        p.held.add("dictation")
        command = {"op": "insert", "id": 7, "session": "s", "text": "中文🌍"}
        with patch.object(bridge.GLib, "timeout_add") as timer:
            p.copy(command)
            p.call.call_args.args[4](None, None)
            pending = timer.call_args.args[1]
            self.assertTrue(pending())
            p.held.clear()
            self.assertTrue(pending())
            p.clipboard_owner = True
            self.assertFalse(pending())
        p.send_paste.assert_called_once_with(command, "/input")
        self.assertEqual(p.clipboard.decode(), "中文🌍")

    def test_only_accepted_request_reaches_continuation(self):
        p = fixture()
        callback = Mock()
        p.requests["/request"] = callback
        args = bridge.GLib.Variant("(ua{sv})", (1, {}))
        p.signal(None, None, "/request", bridge.PREFIX + "Request", "Response", args)
        self.assertIn("denied", callback.call_args.args[1])
        self.assertNotIn("/request", p.requests)


class GnomeShortcutSettingsTests(unittest.TestCase):
    def setUp(self):
        source = bridge.Gio.SettingsSchemaSource.get_default()
        if not source or not source.lookup(bridge.GnomeShortcuts.SCHEMA, True):
            self.skipTest("GNOME settings schemas are not installed")
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            self.shortcuts = bridge.GnomeShortcuts("/usr/bin/true")
        self.settings = self.shortcuts.settings
        self.unrelated = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/personal/"
        self.settings.set_strv("custom-keybindings", [self.unrelated])
        for _, entry, _, _ in self.shortcuts.entries:
            for key in ("name", "command", "binding"):
                entry.reset(key)

    def test_enable_reconnect_disable_preserve_user_entries_and_key_choices(self):
        self.shortcuts.enable()
        paths = list(self.settings.get_strv("custom-keybindings"))
        self.assertEqual(len(paths), 3)
        self.assertEqual(paths[0], self.unrelated)
        entry = self.shortcuts.entries[0][1]
        self.assertIn("--portal-hotkey --toggle-dictation", entry.get_string("command"))
        entry.set_string("binding", "<Super>F9")
        self.shortcuts.disable()
        self.assertEqual(list(self.settings.get_strv("custom-keybindings")), [self.unrelated])
        self.shortcuts.enable()
        self.assertEqual(entry.get_string("binding"), "<Super>F9")
        self.shortcuts.enable()
        self.assertEqual(len(self.settings.get_strv("custom-keybindings")), 3)
        self.shortcuts.disable()

    def test_default_shortcut_conflict_preserves_existing_binding(self):
        other = bridge.Gio.Settings.new_with_path(
            bridge.GnomeShortcuts.SCHEMA + ".custom-keybinding", self.unrelated
        )
        other.set_string("binding", "<Alt><Primary>space")
        try:
            self.shortcuts.enable()
            self.assertEqual(other.get_string("binding"), "<Alt><Primary>space")
            self.assertEqual(self.shortcuts.entries[0][1].get_string("binding"), "")
            self.assertTrue(self.shortcuts.needs_bindings())
            self.shortcuts.disable()
        finally:
            other.reset("binding")

    def test_existing_unrelated_command_is_never_overwritten(self):
        entry = self.shortcuts.entries[0][1]
        entry.set_string("command", "some-other-program")
        with self.assertRaises(RuntimeError):
            self.shortcuts.enable()
        self.assertEqual(entry.get_string("command"), "some-other-program")
        self.assertEqual(list(self.settings.get_strv("custom-keybindings")), [self.unrelated])


if __name__ == "__main__":
    unittest.main()
