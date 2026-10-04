"""Portal lifecycle tests; no desktop session or permission dialogs required."""

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
