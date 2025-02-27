# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2022 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008, 2010 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from gi.repository import GLib, Gdk

from xpra.client.keyboard_helper import KeyboardHelper, log
from xpra.gtk_common.keymap import get_gtk_keymap
from xpra.os_util import is_X11


class GTKKeyboardHelper(KeyboardHelper):

    def __init__(self, *args):
        KeyboardHelper.__init__(self, *args)
        #used for delaying the sending of keymap changes
        #(as we may be getting dozens of such events at a time)
        self._keymap_changing = False
        self._keymap_change_handler_id = None
        self._keymap = None
        display = Gdk.Display.get_default()
        if display:
            self._keymap = Gdk.Keymap.get_for_display(display)
        self.update()
        if self._keymap:
            self._keymap_change_handler_id = self._keymap.connect("keys-changed", self.keymap_changed)

    def keymap_changed(self, *args):
        log("keymap_changed%s", args)
        if self._keymap_change_handler_id:
            self._keymap.disconnect(self._keymap_change_handler_id)
            self._keymap_change_handler_id = None
        display = Gdk.Display.get_default()
        self._keymap = Gdk.Keymap.get_for_display(display)
        if self._keymap_changing:
            #timer due already
            return
        self._keymap_changing = True
        def do_keys_changed():
            #re-register the change handler:
            self._keymap_change_handler_id = self._keymap.connect("keys-changed", self.keymap_changed)
            self._keymap_changing = False
            if self.locked:
                #automatic changes not allowed!
                log.info("ignoring keymap change: layout is locked to '%s'", self.layout_str())
                return
            if self.update() and self.layout:
                log.info("keymap has been changed to '%s'", self.layout_str())
                log.info(" sending updated mappings to the server")
                if self.layout:
                    self.send_layout()
                self.send_keymap()
        GLib.timeout_add(500, do_keys_changed)

    def update(self):
        old_hash = self.hash
        super().update()
        if is_X11():
            try:
                self.keyboard.update_modifier_map(Gdk.Display.get_default(), self.mod_meanings)
            except Exception:
                log.error("error querying modifier map", exc_info=True)
        log("update() modifier_map=%s, old hash=%s, new hash=%s", self.keyboard.modifier_map, old_hash, self.hash)
        return old_hash!=self.hash

    def get_full_keymap(self):
        return get_gtk_keymap()

    def cleanup(self):
        super().cleanup()
        if self._keymap_change_handler_id:
            try:
                self._keymap.disconnect(self._keymap_change_handler_id)
                self._keymap_change_handler_id = None
            except Exception as e:
                log.warn("failed to disconnect keymap change handler: %s", e)


def main():
    #use gtk as display source:
    from xpra.gtk_common.gtk_util import init_display_source
    from xpra.util import print_nested_dict
    from xpra.platform import program_context
    with program_context("GTK-Keyboard", "GTK Keyboard"):
        init_display_source()
        x = GTKKeyboardHelper(None, True, "")
        x.query_xkbmap()
        print_nested_dict(x.get_keymap_properties())

if __name__ == "__main__":
    main()
