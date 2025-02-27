#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2017-2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import os.path
import subprocess
from time import monotonic

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango

from xpra.util import envint, net_utf8
from xpra.os_util import bytestostr, WIN32, OSX
from xpra.gtk_common.gobject_compat import register_os_signals
from xpra.child_reaper import getChildReaper
from xpra.net.file_transfer import ACCEPT, OPEN, DENY
from xpra.simple_stats import std_unit, std_unit_dec
from xpra.gtk_common.gtk_util import (
    add_close_accel, scaled_image,
    TableBuilder, get_icon_pixbuf,
    )
from xpra.platform.gui import set_window_progress
from xpra.platform.paths import get_download_dir
from xpra.log import Logger

log = Logger("gtk", "file")

URI_MAX_WIDTH = envint("XPRA_URI_MAX_WIDTH", 320)
REMOVE_ENTRY_DELAY = envint("XPRA_FILE_REMOVE_ENTRY_DELAY", 5)


_instance = None
def getOpenRequestsWindow(show_file_upload_cb=None, cancel_download=None):
    global _instance
    if _instance is None:
        _instance = OpenRequestsWindow(show_file_upload_cb, cancel_download)
    return _instance


class OpenRequestsWindow:

    def __init__(self, show_file_upload_cb=None, cancel_download=None):
        self.show_file_upload_cb = show_file_upload_cb
        self.cancel_download = cancel_download
        self.populate_timer = None
        self.table = None
        self.requests = []
        self.expire_labels = {}
        self.progress_bars = {}
        self.window = Gtk.Window()
        self.window.set_border_width(20)
        self.window.connect("delete-event", self.close)
        self.window.set_default_size(400, 150)
        self.window.set_title("Transfers")

        icon_pixbuf = get_icon_pixbuf("download.png")
        if icon_pixbuf:
            self.window.set_icon(icon_pixbuf)
        self.window.set_position(Gtk.WindowPosition.CENTER)

        vbox = Gtk.VBox(homogeneous=False, spacing=0)
        vbox.set_spacing(10)

        self.alignment = Gtk.Alignment(xalign=0.5, yalign=0.5, xscale=1.0, yscale=1.0)
        vbox.pack_start(self.alignment, expand=True, fill=True)

        # Buttons:
        hbox = Gtk.HBox(homogeneous=False, spacing=20)
        vbox.pack_start(hbox)
        def btn(label, callback, icon_name=None):
            b = self.btn(label, callback, icon_name)
            hbox.pack_start(b)
        if self.show_file_upload_cb:
            btn("Upload", self.show_file_upload_cb, "upload.png")
        btn("Close", self.close, "quit.png")
        downloads = get_download_dir()
        if downloads:
            btn("Show Downloads Folder", self.show_downloads, "browse.png")

        def accel_close(*_args):
            self.close()
        add_close_accel(self.window, accel_close)
        vbox.show_all()
        self.window.vbox = vbox
        self.window.add(vbox)
        self.populate_table()

    def btn(self, label, callback, icon_name=None):
        btn = Gtk.Button(label=label)
        settings = btn.get_settings()
        settings.set_property('gtk-button-images', True)
        btn.connect("clicked", callback)
        if icon_name:
            icon = get_icon_pixbuf(icon_name)
            if icon:
                btn.set_image(scaled_image(icon, 24))
        return btn


    def add_request(self, cb_answer, send_id, dtype, url, filesize, printit, openit, timeout):
        expires = monotonic()+timeout
        self.requests.append((cb_answer, net_utf8(send_id), net_utf8(dtype), net_utf8(url), filesize, printit, openit, expires))
        self.populate_table()
        if not self.populate_timer:
            self.schedule_timer()

    def update_expires_label(self):
        expired = 0
        for label, expiry in self.expire_labels.values():
            seconds = max(0, expiry-monotonic())
            label.set_text("%i" % seconds)
            if seconds==0:
                expired += 1
        if expired:
            self.populate_table()
            self.window.resize(1, 1)
        if self.expire_labels:
            return True
        self.populate_timer = 0
        return False

    def populate_table(self):
        if self.table:
            self.alignment.remove(self.table)
        #remove expired requests:
        now = monotonic()
        self.requests = [x for x in self.requests if x[-1]>now]
        self.expire_labels = {}
        tb = TableBuilder(rows=1, columns=4, row_spacings=15)
        #generate a new table:
        self.table = tb.get_table()
        def l(s=""):
            return Gtk.Label(label=s)
        if not self.requests:
            tb.add_row(l("No requests pending"))
        else:
            headers = [l("URL / Filename"), l(), l("Expires in"), l("Action")]
            tb.add_row(*headers)
            for cb_answer, send_id, dtype, url, filesize, printit, openit, expires in self.requests:
                details = ""
                if dtype=="file" and filesize>0:
                    details = "%sB" % std_unit_dec(filesize)
                expires_label = l()
                self.expire_labels[send_id] = (expires_label, expires)
                buttons = self.action_buttons(cb_answer, send_id, dtype, printit, openit)
                s = bytestostr(url)
                main_label = l(s)
                if dtype=="url" and s.find("?")>0 and len(s)>48:
                    parts = s.split("?", 1)
                    main_label.set_label(parts[0]+"?..")
                    main_label.set_tooltip_text(s)
                main_label.set_line_wrap(True)
                main_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)  # @UndefinedVariable
                main_label.set_size_request(URI_MAX_WIDTH, -1)
                main_label.set_selectable(True)
                items = (main_label, l(details), expires_label, buttons)
                tb.add_row(*items)
            self.update_expires_label()
        self.alignment.add(self.table)
        self.table.show_all()

    def remove_entry(self, send_id, can_close=True):
        self.expire_labels.pop(send_id, None)
        self.requests = [x for x in self.requests if x[1]!=send_id]
        self.progress_bars.pop(send_id, None)
        if not self.requests and can_close:
            self.close()
        else:
            self.populate_table()
            self.window.resize(1, 1)

    def action_buttons(self, cb_answer, send_id, dtype, printit, openit):
        hbox = Gtk.HBox()
        def remove_entry(can_close=False):
            self.remove_entry(send_id, can_close)
        def stop(*args):
            log(f"stop{args}")
            remove_entry(True)
            self.cancel_download(send_id, "User cancelled")
        def show_progressbar(position=0, total=1):
            expire = self.expire_labels.pop(send_id, None)
            if expire:
                expire_label = expire[0]
                expire_label.set_text("")
            for b in hbox.get_children():
                hbox.remove(b)
            stop_btn = None
            if position!=None:
                stop_btn = self.btn("Stop", stop, "close.png")
                hbox.pack_start(stop_btn)
            pb = Gtk.ProgressBar()
            hbox.set_spacing(20)
            hbox.pack_start(pb)
            hbox.show_all()
            pb.set_size_request(420, 30)
            if position!=None and total>0:
                pb.set_fraction(position/total)
                pb.set_text("%sB of %s" % (std_unit(position), std_unit(total)))
            self.progress_bars[send_id] = [stop_btn, pb, position, total]
        def cancel(*args):
            log(f"cancel{args}")
            remove_entry(True)
            cb_answer(DENY)
        def accept(*args):
            log(f"accept{args}")
            remove_entry(False)
            cb_answer(ACCEPT, True)
        def remote(*args):
            log(f"remote{args}")
            remove_entry(True)
            cb_answer(OPEN)
        def progress(*args):
            log(f"progress{args}")
            cb_answer(ACCEPT, False)
            show_progressbar()
        def progressaccept(*args):
            log(f"progressaccept{args}")
            cb_answer(ACCEPT, True)
            show_progressbar()
        pbd = self.progress_bars.pop(send_id, None)
        if pbd:
            #we already accepted this one, show stop + progressbar
            show_progressbar(pbd[2], pbd[3])
            return hbox
        cancel_btn = self.btn("Cancel", cancel, "close.png")
        hbox.pack_start(cancel_btn)
        if bytestostr(dtype)=="url":
            hbox.pack_start(self.btn("Open Locally", accept, "open.png"))
            hbox.pack_start(self.btn("Open on server", remote))
        elif printit:
            hbox.pack_start(self.btn("Print", progressaccept, "printer.png"))
        else:
            hbox.pack_start(self.btn("Download", progress, "download.png"))
            if openit:
                hbox.pack_start(self.btn("Download and Open", progressaccept, "open.png"))
                hbox.pack_start(self.btn("Open on server", remote))
        return hbox

    def schedule_timer(self):
        if not self.populate_timer and self.expire_labels:
            self.update_expires_label()
            self.populate_timer = GLib.timeout_add(1000, self.update_expires_label)

    def cancel_timer(self):
        if self.populate_timer:
            GLib.source_remove(self.populate_timer)
            self.populate_timer = 0


    def transfer_progress_update(self, send=True, transfer_id=0, elapsed=0, position=0, total=0, error=None):
        pbd = self.progress_bars.get(transfer_id)
        if not pbd:
            #we're not tracking this transfer: no progress bar
            return
        stop_btn, pb = pbd[:2]
        log("transfer_progress_update%s pb=%s", (send, transfer_id, elapsed, position, total, error), pb)
        if error:
            pbd[2] = None
            if stop_btn:
                stop_btn.hide()
            pb.set_text("Error: %s, file transfer aborted" % error)
            GLib.timeout_add(REMOVE_ENTRY_DELAY*1000, self.remove_entry, transfer_id)
            return
        else:
            pbd[2] = position
            pbd[3] = total
        if pb and total>0:
            pb.set_fraction(position/total)
            pb.set_text("%sB of %s" % (std_unit(position), std_unit(total)))
            pb.set_show_text(True)
        if position==total:
            if stop_btn:
                stop_btn.hide()
            pb.set_text("Complete: %sB" % std_unit(total))
            pb.set_show_text(True)
            GLib.timeout_add(REMOVE_ENTRY_DELAY*1000, self.remove_entry, transfer_id)
        #totals:
        position = sum(pbd[2] for pbd in self.progress_bars.values())
        total = sum(pbd[3] for pbd in self.progress_bars.values())
        set_window_progress(self.window, round(100*position/total))


    def show(self):
        log("show()")
        self.window.show_all()
        self.window.present()
        self.schedule_timer()

    def hide(self):
        log("hide()")
        self.window.hide()
        self.cancel_timer()

    def close(self, *args):
        log("close%s", args)
        self.hide()
        return True

    def destroy(self, *args):
        log("destroy%s", args)
        self.cancel_timer()
        if self.window:
            self.window.destroy()
            self.window = None

    def show_downloads(self, _btn):
        downloads = os.path.expanduser(get_download_dir())
        if WIN32:
            os.startfile(downloads) #@UndefinedVariable pylint: disable=no-member
            return
        if OSX:
            cmd = ["open", downloads]
        else:
            cmd = ["xdg-open", downloads]
        try:
            proc = subprocess.Popen(cmd)
        except Exception as e:
            log("show_downloads()", exc_info=True)
            log.error("Error: failed to open 'Downloads' folder:")
            log.error(" %s", e)
        else:
            getChildReaper().add_process(proc, "show-downloads", cmd, ignore=True, forget=True)


    def run(self):
        log("run()")
        Gtk.main()
        log("run() Gtk.main done")

    def quit(self, *args):
        log("quit%s", args)
        self.destroy()
        Gtk.main_quit()


def main():     # pragma: no cover
    from xpra.platform import program_context
    from xpra.platform.gui import init as gui_init, ready as gui_ready
    gui_init()
    with program_context("Start-New-Command", "Start New Command"):
        #logging init:
        if "-v" in sys.argv:
            from xpra.log import enable_debug_for
            enable_debug_for("util")

        app = OpenRequestsWindow()
        def cb(accept):
            print("callback: %s" % (accept,))
        app.add_request(cb, "1", "file", "someimage.png", 16384, False, True, 10)
        app.add_request(cb, "2", "file", "otherimage.png", 16384, False, True, 100)
        app.add_request(cb, "3", "file", "document.pdf", 32768, True, False, 200)
        app.add_request(cb, "4", "url", "https://xpra.org/", 0, False, True, 300)
        app.hide = app.quit
        register_os_signals(app.quit)
        try:
            gui_ready()
            app.show()
            app.run()
        except KeyboardInterrupt:
            pass
        return 0


if __name__ == "__main__":  # pragma: no cover
    v = main()
    sys.exit(v)
