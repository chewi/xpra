# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2010-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
#pylint: disable-msg=E1101

import os.path
from time import monotonic

from xpra.util import parse_scaling_value, csv, from0to100, net_utf8, typedict
from xpra.os_util import load_binary_file
from xpra.simple_stats import std_unit
from xpra.scripts.config import parse_bool, FALSE_OPTIONS, TRUE_OPTIONS
from xpra.server.control_command import ArgsControlCommand, ControlError
from xpra.server.mixins.stub_server_mixin import StubServerMixin
from xpra.log import Logger

log = Logger("command")

TOGGLE_FEATURES = (
    "bell", "randr", "cursors", "notifications", "dbus-proxy", "clipboard",
    "start-new-commands", "client-shutdown", "webcam",
    )


class ServerBaseControlCommands(StubServerMixin):
    """
    Control commands for ServerBase
    """

    def setup(self):
        self.add_control_commands()


    def add_control_commands(self):
        def parse_boolean_value(v):
            if str(v).lower() in TRUE_OPTIONS:
                return True
            if str(v).lower() in FALSE_OPTIONS:
                return False
            raise ControlError("a boolean is required, not %s" % v)
        def parse_4intlist(v):
            if not v:
                return []
            l = []
            #ie: v = " (0,10,100,20), (200,300,20,20)"
            while v:
                v = v.strip().strip(",").strip()    #ie: "(0,10,100,20)"
                lp = v.find("(")
                assert lp==0, "invalid leading characters: %s" % v[:lp]
                rp = v.find(")")
                assert (lp+1)<rp
                item = v[lp+1:rp].strip()           #"0,10,100,20"
                items = [int(x) for x in item]      # 0,10,100,20
                assert len(items)==4, "expected 4 numbers but got %i" % len(items)
                l.append(items)
            return l

        for cmd in (
            ArgsControlCommand("focus",                 "give focus to the window id",      validation=[int]),
            ArgsControlCommand("map",                   "maps the window id",               validation=[int]),
            ArgsControlCommand("unmap",                 "unmaps the window id",             validation=[int]),
            #window source:
            ArgsControlCommand("suspend",               "suspend screen updates",           max_args=0),
            ArgsControlCommand("resume",                "resume screen updates",            max_args=0),
            ArgsControlCommand("ungrab",                "cancels any grabs",                max_args=0),
            #server globals:
            ArgsControlCommand("readonly",              "set readonly state for client(s)", min_args=1, max_args=1, validation=[parse_boolean_value]),
            ArgsControlCommand("idle-timeout",          "set the idle tiemout",             validation=[int]),
            ArgsControlCommand("server-idle-timeout",   "set the server idle timeout",      validation=[int]),
            ArgsControlCommand("start-env",             "modify the environment used to start new commands", min_args=2),
            ArgsControlCommand("start",                 "executes the command arguments in the server context", min_args=1),
            ArgsControlCommand("start-child",           "executes the command arguments in the server context, as a 'child' (honouring exit-with-children)", min_args=1),
            ArgsControlCommand("toggle-feature",        "toggle a server feature on or off, one of: %s" % csv(TOGGLE_FEATURES), min_args=1, max_args=2, validation=[str, parse_boolean_value]),
            #network and transfers:
            ArgsControlCommand("print",                 "sends the file to the client(s) for printing", min_args=1),
            ArgsControlCommand("open-url",              "open the URL on the client(s)",    min_args=1, max_args=2),
            ArgsControlCommand("send-file",             "sends the file to the client(s)",  min_args=1, max_args=4),
            ArgsControlCommand("send-notification",     "sends a notification to the client(s)",  min_args=4, max_args=5, validation=[int]),
            ArgsControlCommand("close-notification",    "send the request to close an existing notification to the client(s)", min_args=1, max_args=2, validation=[int]),
            ArgsControlCommand("compression",           "sets the packet compressor",       min_args=1, max_args=1),
            ArgsControlCommand("encoder",               "sets the packet encoder",          min_args=1, max_args=1),
            ArgsControlCommand("clipboard-direction",   "restrict clipboard transfers",     min_args=1, max_args=1),
            ArgsControlCommand("clipboard-limits",      "restrict clipboard transfers size", min_args=2, max_args=2, validation=[int, int]),
            ArgsControlCommand("set-lock",              "modify the lock attribute",        min_args=1, max_args=1),
            ArgsControlCommand("set-sharing",           "modify the sharing attribute",     min_args=1, max_args=1),
            ArgsControlCommand("set-ui-driver",         "set the client connection driving the session", min_args=1, max_args=1),
            #session and clients:
            ArgsControlCommand("client",                "forwards a control command to the client(s)", min_args=1),
            ArgsControlCommand("client-property",       "set a client property",            min_args=4, max_args=5, validation=[int]),
            ArgsControlCommand("name",                  "set the session name",             min_args=1, max_args=1),
            ArgsControlCommand("key",                   "press or unpress a key",           min_args=1, max_args=2),
            ArgsControlCommand("sound-output",          "control sound forwarding",         min_args=1, max_args=2),
            #windows:
            ArgsControlCommand("workspace",             "move a window to a different workspace", min_args=2, max_args=2, validation=[int, int]),
            ArgsControlCommand("close",                 "close a window",                   min_args=1, max_args=1, validation=[int]),
            ArgsControlCommand("delete",                "delete a window",                   min_args=1, max_args=1, validation=[int]),
            ArgsControlCommand("move",                  "move a window",                    min_args=3, max_args=3, validation=[int, int, int]),
            ArgsControlCommand("resize",                "resize a window",                  min_args=3, max_args=3, validation=[int, int, int]),
            ArgsControlCommand("moveresize",            "move and resize a window",         min_args=5, max_args=5, validation=[int, int, int, int, int]),
            ArgsControlCommand("scaling-control",       "set the scaling-control aggressiveness (from 0 to 100)", min_args=1, validation=[from0to100]),
            ArgsControlCommand("scaling",               "set a specific scaling value",     min_args=1, validation=[parse_scaling_value]),
            ArgsControlCommand("auto-refresh",          "set a specific auto-refresh value", min_args=1, validation=[float]),
            ArgsControlCommand("refresh",               "refresh some or all windows",      min_args=0),
            ArgsControlCommand("encoding",              "picture encoding",                 min_args=2),
            ArgsControlCommand("request-update",        "request a screen update using a specific encoding",  min_args=3),
            ArgsControlCommand("video-region-enabled",  "enable video region",              min_args=2, max_args=2, validation=[int, parse_boolean_value]),
            ArgsControlCommand("video-region-detection","enable video detection",           min_args=2, max_args=2, validation=[int, parse_boolean_value]),
            ArgsControlCommand("video-region-exclusion-zones","set window regions to exclude from video regions: 'WID,(x,y,w,h),(x,y,w,h),..', ie: '1 (0,10,100,20),(200,300,20,20)'",  min_args=2, max_args=2, validation=[int, parse_4intlist]),
            ArgsControlCommand("video-region",          "set the video region",             min_args=5, max_args=5, validation=[int, int, int, int, int]),
            ArgsControlCommand("reset-video-region",    "reset video region heuristics",    min_args=1, max_args=1, validation=[int]),
            ArgsControlCommand("lock-batch-delay",      "set a specific batch delay for a window",       min_args=2, max_args=2, validation=[int, int]),
            ArgsControlCommand("unlock-batch-delay",    "let the heuristics calculate the batch delay again for a window (following a 'lock-batch-delay')",  min_args=1, max_args=1, validation=[int]),
            ArgsControlCommand("remove-window-filters", "remove all window filters",        min_args=0, max_args=0),
            ArgsControlCommand("add-window-filter",     "add a window filter",              min_args=4, max_args=5),
            ):
            cmd.do_run = getattr(self, "control_command_%s" % cmd.name.replace("-", "_"))
            self.control_commands[cmd.name] = cmd
        #encoding bits:
        for name in (
            "quality", "min-quality", "max-quality",
            "speed", "min-speed", "max-speed",
            ):
            fn = getattr(self, "control_command_%s" % name.replace("-", "_"))
            self.control_commands[name] = ArgsControlCommand(name, "set encoding %s (from 0 to 100)" % name, run=fn, min_args=1, validation=[from0to100])


    #########################################
    # Control Commands
    #########################################
    def control_command_focus(self, wid):
        if self.readonly:
            return
        assert type(wid)==int, "argument should have been an int, but found %s" % type(wid)
        self._focus(None, wid, None)
        return "gave focus to window %s" % wid

    def control_command_map(self, wid):
        if self.readonly:
            return
        assert type(wid)==int, "argument should have been an int, but found %s" % type(wid)
        window = self._id_to_window.get(wid)
        assert window, "window %i not found" % wid
        if window.is_tray():
            return "cannot map tray window %s" % wid
        if window.is_OR():
            return "cannot map override redirect window %s" % wid
        window.show()
        #window.set_owner(dm)
        #iconic = window.get_property("iconic")
        #if iconic:
        #    window.set_property("iconic", False)
        #w, h = window.get_geometry()[2:4]
        #self.refresh_window_area(window, 0, 0, w, h)
        self.repaint_root_overlay()
        return "mapped window %s" % wid

    def control_command_unmap(self, wid):
        if self.readonly:
            return
        assert type(wid)==int, "argument should have been an int, but found %s" % type(wid)
        window = self._id_to_window.get(wid)
        assert window, "window %i not found" % wid
        if window.is_tray():
            return "cannot map tray window %s" % wid
        if window.is_OR():
            return "cannot map override redirect window %s" % wid
        window.hide()
        self.repaint_root_overlay()
        return "unmapped window %s" % wid

    def control_command_suspend(self):
        for csource in tuple(self._server_sources.values()):
            csource.suspend(True, self._id_to_window)
        return "suspended %s clients" % len(self._server_sources)

    def control_command_resume(self):
        for csource in tuple(self._server_sources.values()):
            csource.resume(True, self._id_to_window)
        return "resumed %s clients" % len(self._server_sources)

    def control_command_ungrab(self):
        for csource in tuple(self._server_sources.values()):
            csource.pointer_ungrab(-1)
        return "ungrabbed %s clients" % len(self._server_sources)

    def control_command_readonly(self, onoff):
        log("control_command_readonly(%s)", onoff)
        self.readonly = onoff
        msg = "server readonly: %s" % onoff
        log.info(msg)
        return msg

    def control_command_idle_timeout(self, t):
        self.idle_timeout = t
        for csource in tuple(self._server_sources.values()):
            csource.idle_timeout = t
            csource.schedule_idle_timeout()
        return "idle-timeout set to %s" % t

    def control_command_server_idle_timeout(self, t):
        self.server_idle_timeout = t
        reschedule = len(self._server_sources)==0
        self.reset_server_timeout(reschedule)
        return "server-idle-timeout set to %s" % t


    def control_command_start_env(self, action="set", var_name="", value=None):
        assert var_name, "the environment variable name must be specified"
        if action=="unset":
            assert value is None, "invalid number of arguments for %s" % action
            if self.start_env.pop(var_name, None) is None:
                return "%r is not set" % var_name
            return "%r unset" % var_name
        if action=="set":
            assert value, "the value must be specified"
            self.start_env[var_name] = value
            return "%s=%s" % (var_name, value)
        return "invalid start-env subcommand %r" % action


    def control_command_start(self, *args):
        return self.do_control_command_start(True, *args)
    def control_command_start_child(self, *args):
        return self.do_control_command_start(False, *args)
    def do_control_command_start(self, ignore, *args):
        if not self.start_new_commands:
            raise ControlError("this feature is currently disabled")
        proc = self.start_command(" ".join(args), args, ignore, shell=True)
        if not proc:
            raise ControlError("failed to start new child command %s" % str(args))
        return "new %scommand started with pid=%s" % (["child ", ""][ignore], proc.pid)

    def control_command_toggle_feature(self, feature, state=None):
        log("control_command_toggle_feature(%s, %s)", feature, state)
        if feature not in TOGGLE_FEATURES:
            msg = "invalid feature '%s'" % feature
            log.warn(msg)
            return msg
        fn = feature.replace("-", "_")
        if not hasattr(self, feature):
            msg = "attribute '%s' not found - bug?" % feature
            log.warn(msg)
            return msg
        cur = getattr(self, fn, None)
        if state is None:
            #if the new state is not specified, just negate the value
            state = not cur
        setattr(self, fn, state)
        self.setting_changed(feature, state)
        return "%s set to %s" % (feature, state)

    def _control_get_sources(self, client_uuids_str, _attr=None):
        #find the client uuid specified as a string:
        if client_uuids_str=="UI":
            sources = [ss for ss in self._server_sources.values() if ss.ui_client]
            client_uuids = [ss.uuid for ss in sources]
            notfound = ()
        elif client_uuids_str=="*":
            sources = self._server_sources.values()
            client_uuids = [ss.uuid for ss in sources]
        else:
            client_uuids = client_uuids_str.split(",")
            sources = [ss for ss in self._server_sources.values() if ss.uuid in client_uuids]
            uuids = tuple(ss.uuid for ss in sources)
            notfound = any(x for x in client_uuids if x not in uuids)
            if notfound:
                log.warn("client connection not found for uuid(s): %s", notfound)
        return sources

    def control_command_send_notification(self, nid, title, message, client_uuids):
        if not self.notifications:
            msg = "notifications are disabled"
            log(msg)
            return msg
        sources = self._control_get_sources(client_uuids)
        log("control_command_send_notification(%i, %s, %s, %s) will send to sources %s (matching %s)",
            nid, title, message, client_uuids, sources, client_uuids)
        count = 0
        for source in sources:
            if source.notify(0, nid, "control channel", 0, "", title, message, [], {}, 10, ""):
                count += 1
        msg = "notification id %i: message sent to %i clients" % (nid, count)
        log(msg)
        return msg

    def control_command_close_notification(self, nid, client_uuids):
        if not self.notifications:
            msg = "notifications are disabled"
            log(msg)
            return msg
        sources = self._control_get_sources(client_uuids)
        log("control_command_close_notification(%s, %s) will send to %s", nid, client_uuids, sources)
        for source in sources:
            source.notify_close(nid)
        msg = "notification id %i: close request sent to %i clients" % (nid, len(sources))
        log(msg)
        return msg


    def control_command_open_url(self, url, client_uuids="*"):
        #find the clients:
        sources = self._control_get_sources(client_uuids)
        if not sources:
            raise ControlError("no clients found matching: %s" % client_uuids)
        clients = 0
        for ss in sources:
            if hasattr(ss, "send_open_url"):
                if ss.send_open_url(url):
                    clients += 1
        return "url sent to %i clients" % clients

    def control_command_send_file(self, filename, openit="open", client_uuids="*", maxbitrate=0):
        #we always get the values as strings from the command interface,
        #but those may actually be utf8 encoded binary strings,
        #so we may have to do an ugly roundtrip:
        filename = net_utf8(filename)
        openit = str(openit).lower() in ("open", "true", "1")
        return self.do_control_file_command("send file", client_uuids, filename, "file_transfer", (False, openit))

    def control_command_print(self, filename, printer="", client_uuids="*", maxbitrate=0, title="", *options_strs):
        #FIXME: printer and bitrate are ignored
        #parse options into a dict:
        options = {}
        for arg in options_strs:
            argp = arg.split("=", 1)
            if len(argp)==2 and len(argp[0])>0:
                options[argp[0]] = argp[1]
        return self.do_control_file_command("print", client_uuids, filename, "printing", (True, True, options))

    def do_control_file_command(self, command_type, client_uuids, filename, source_flag_name, send_file_args):
        #find the clients:
        sources = self._control_get_sources(client_uuids)
        if not sources:
            raise ControlError("no clients found matching: %s" % client_uuids)
        def checksize(file_size):
            if file_size>self.file_transfer.file_size_limit:
                raise ControlError("file '%s' is too large: %sB (limit is %sB)" % (
                    filename, std_unit(file_size), std_unit(self.file_transfer.file_size_limit)))

        #find the file and load it:
        actual_filename = os.path.abspath(os.path.expanduser(filename))
        try:
            stat = os.stat(actual_filename)
            log("os.stat(%s)=%s", actual_filename, stat)
        except os.error:
            log("os.stat(%s)", actual_filename, exc_info=True)
        else:
            checksize(stat.st_size)
        if not os.path.exists(actual_filename):
            raise ControlError("file '%s' does not exist" % filename)
        data = load_binary_file(actual_filename)
        if data is None:
            raise ControlError("failed to load '%s'" % actual_filename)
        #verify size:
        file_size = len(data)
        checksize(file_size)
        #send it to each client:
        for ss in sources:
            #ie: ServerSource.file_transfer (found in FileTransferAttributes)
            if not getattr(ss, source_flag_name, False):
                #skip the warning if the client is not interactive
                #(for now just check for 'top' client):
                if ss.client_type=="top":
                    l = log
                else:
                    l = log.warn
                l("Warning: cannot %s '%s' to %s client", command_type, filename, ss.client_type)
                l(" client %s does not support this feature", ss.uuid)
            elif file_size>ss.file_size_limit:
                log.warn("Warning: cannot %s '%s'", command_type, filename)
                log.warn(" client %s file size limit is %sB (file is %sB)",
                         ss, std_unit(ss.file_size_limit), std_unit(file_size))
            else:
                ss.send_file(filename, "", data, file_size, *send_file_args)
        return "%s of '%s' to %s initiated" % (command_type, filename, client_uuids)


    def control_command_remove_window_filters(self):
        #modify the existing list object,
        #which is referenced by all the sources
        l = len(self.window_filters)
        self.window_filters[:] = []
        return "removed %i window-filters" % l

    def control_command_add_window_filter(self, object_name, property_name, operator, value, client_uuids=""):
        from xpra.server.window import filters  #pylint: disable=import-outside-toplevel
        window_filter = filters.get_window_filter(object_name, property_name, operator, value)
        #log("%s%s=%s", filters.get_window_filter, (object_name, property_name, operator, value), window_filter)
        if client_uuids=="*":
            #applies to all sources:
            self.window_filters.append(("*", window_filter))
        else:
            for client_uuid in client_uuids.split(","):
                self.window_filters.append((client_uuid, window_filter))
        return "added window-filter: %s for client uuids=%s" % (window_filter, client_uuids)


    def control_command_compression(self, compression):
        c = compression.lower()
        from xpra.net import compression    #pylint: disable=import-outside-toplevel
        opts = compression.get_enabled_compressors()    #ie: [lz4, zlib]
        if c not in opts:
            raise ControlError("compressor argument must be one of: %s" % csv(opts))
        for cproto in tuple(self._server_sources.keys()):
            cproto.enable_compressor(c)
        self.all_send_client_command("enable_%s" % c)
        return "compressors set to %s" % compression

    def control_command_encoder(self, encoder):
        e = encoder.lower()
        from xpra.net import packet_encoding  #pylint: disable=import-outside-toplevel
        opts = packet_encoding.get_enabled_encoders()   #ie: [rencode, rencodeplus, bencode, yaml]
        if e not in opts:
            raise ControlError("encoder argument must be one of: %s" % csv(opts))
        for cproto in tuple(self._server_sources.keys()):
            cproto.enable_encoder(e)
        self.all_send_client_command("enable_%s" % e)
        return "encoders set to %s" % encoder


    def all_send_client_command(self, *client_command):
        """ forwards the command to all clients """
        for source in tuple(self._server_sources.values()):
            # forwards to *the* client, if there is *one*
            if client_command[0] not in source.control_commands:
                log.info("client command '%s' not forwarded to client %s (not supported)", client_command, source)
            else:
                source.send_client_command(*client_command)

    def control_command_client(self, *args):
        self.all_send_client_command(*args)
        return "client control command %s forwarded to clients" % str(args)

    def control_command_client_property(self, wid, uuid, prop, value, conv=None):
        wid = int(wid)
        conv_fn = {
            "int"   : int,
            "float" : float,
            ""      : str,
            }.get(conv)
        assert conv_fn
        typeinfo = "%s " % (conv or "string")
        value = conv_fn(value)
        self.client_properties.setdefault(wid, {}).setdefault(uuid, {})[prop] = value
        return "property '%s' set to %s value '%s' for window %i, client %s" % (prop, typeinfo, value, wid, uuid)

    def control_command_name(self, name):
        self.session_name = name
        log.info("changed session name: %s", self.session_name)
        #self.all_send_client_command("name", name)    not supported by any clients, don't bother!
        self.setting_changed("session_name", name)
        self.mdns_update()
        return "session name set to %s" % name

    def _ws_from_args(self, *args):
        #converts the args to valid window ids,
        #then returns all the window sources for those wids
        if len(args)==0 or len(args)==1 and args[0]=="*":
            #default to all if unspecified:
            wids = tuple(self._id_to_window.keys())
        else:
            wids = []
            for x in args:
                try:
                    wid = int(x)
                except ValueError:
                    raise ControlError("invalid window id: %s" % x) from None
                if wid in self._id_to_window:
                    wids.append(wid)
                else:
                    log("window id %s does not exist", wid)
        wss = []
        for csource in tuple(self._server_sources.values()):
            for wid in wids:
                ws = csource.window_sources.get(wid)
                window = self._id_to_window.get(wid)
                if window and ws:
                    wss.append(ws)
        return wss


    def _set_encoding_property(self, name, value, *wids):
        for ws in self._ws_from_args(*wids):
            fn = getattr(ws, "set_%s" % name.replace("-", "_"))   #ie: "set_quality"
            fn(value)
        #now also update the defaults:
        for csource in tuple(self._server_sources.values()):
            csource.default_encoding_options[name] = value
        return "%s set to %i" % (name, value)

    def control_command_quality(self, quality, *wids):
        return self._set_encoding_property("quality", quality, *wids)
    def control_command_min_quality(self, min_quality, *wids):
        return self._set_encoding_property("min-quality", min_quality, *wids)
    def control_command_max_quality(self, max_quality, *wids):
        return self._set_encoding_property("max-quality", max_quality, *wids)
    def control_command_speed(self, speed, *wids):
        return self._set_encoding_property("speed", speed, *wids)
    def control_command_min_speed(self, min_speed, *wids):
        return self._set_encoding_property("min-speed", min_speed, *wids)
    def control_command_max_speed(self, max_speed, *wids):
        return self._set_encoding_property("max-speed", max_speed, *wids)

    def control_command_auto_refresh(self, auto_refresh, *wids):
        delay = int(float(auto_refresh)*1000.0)      # ie: 0.5 -> 500 (milliseconds)
        for ws in self._ws_from_args(*wids):
            ws.set_auto_refresh_delay(auto_refresh)
        return "auto-refresh delay set to %sms for windows %s" % (delay, wids)

    def control_command_refresh(self, *wids):
        for ws in self._ws_from_args(*wids):
            ws.full_quality_refresh({})
        return "refreshed windows %s" % str(wids)

    def control_command_scaling_control(self, scaling_control, *wids):
        for ws in tuple(self._ws_from_args(*wids)):
            ws.set_scaling_control(scaling_control)
            ws.refresh()
        return "scaling-control set to %s on windows %s" % (scaling_control, wids)

    def control_command_scaling(self, scaling, *wids):
        for ws in tuple(self._ws_from_args(*wids)):
            ws.set_scaling(scaling)
            ws.refresh()
        return "scaling set to %s on windows %s" % (str(scaling), wids)

    def control_command_encoding(self, encoding, *args):
        if encoding in ("add", "remove"):
            cmd = encoding
            assert len(args)>0
            encoding = args[0]
            wids = args[1:]
            for ws in tuple(self._ws_from_args(*wids)):
                encodings = list(ws.encodings)
                core_encodings = list(ws.core_encodings)
                for l in (encodings, core_encodings):
                    if cmd=="add" and encoding not in l:
                        log("adding %s to %s for %s", encoding, l, ws)
                        l.append(encoding)
                    elif cmd=="remove" and encoding in l:
                        log("removing %s from %s for %s", encoding, l, ws)
                        l.remove(encoding)
                    else:
                        continue
                ws.encodings = tuple(encodings)
                ws.core_encodings = tuple(core_encodings)
                ws.do_set_client_properties(typedict())
                ws.refresh()
            return "%s %s" % (["removed", "added"][cmd=="add"], encoding)

        strict = None       #means no change
        if encoding in ("strict", "nostrict"):
            strict = encoding=="strict"
            encoding = args[0]
            wids = args[1:]
        elif len(args)>0 and args[0] in ("strict", "nostrict"):
            #remove "strict" marker
            strict = args[0]=="strict"
            wids = args[1:]
        else:
            wids = args
        for ws in tuple(self._ws_from_args(*wids)):
            ws.set_new_encoding(encoding, strict)
            ws.refresh()
        return "set encoding to %s%s for windows %s" % (encoding, ["", " (strict)"][int(strict or 0)], wids)

    def control_command_request_update(self, encoding, geom, *args):
        wids = args
        now = monotonic()
        options = {
            "auto_refresh" : True,
            "av-delay" : 0,
            }
        log("request-update using %r, geometry=%s, windows(%s)=%s",
                 encoding, geom, wids, self._ws_from_args(*wids))
        for ws in tuple(self._ws_from_args(*wids)):
            if geom=="all":
                x = y = 0
                w, h = ws.window_dimensions
            else:
                x, y, w, h = (int(x) for x in geom.split(","))
            ws.process_damage_region(now, x, y, w, h, encoding, options)
        return "damage requested"

    def control_command_clipboard_direction(self, direction, *_args):
        ch = self._clipboard_helper
        assert self.clipboard and ch
        direction = direction.lower()
        DIRECTIONS = ("to-server", "to-client", "both", "disabled")
        assert direction in DIRECTIONS, "invalid direction '%s', must be one of %s" % (direction, csv(DIRECTIONS))
        self.clipboard_direction = direction
        can_send = direction in ("to-server", "both")
        can_receive = direction in ("to-client", "both")
        ch.set_direction(can_send, can_receive)
        msg = "clipboard direction set to '%s'" % direction
        log(msg)
        self.setting_changed("clipboard-direction", direction)
        return msg

    def control_command_clipboard_limits(self, max_send, max_recv, *_args):
        ch = self._clipboard_helper
        assert self.clipboard and ch
        ch.set_limits(max_send, max_recv)
        msg = "clipboard send limit set to %d, recv limit set to %d (single copy/paste)" % (max_send, max_recv)
        log(msg)
        self.setting_changed("clipboard-limits", {'send': max_send, 'recv': max_recv})
        return msg

    def _control_video_subregions_from_wid(self, wid):
        if wid not in self._id_to_window:
            raise ControlError("invalid window %i" % wid)
        video_subregions = []
        for ws in self._ws_from_args(wid):
            vs = getattr(ws, "video_subregion", None)
            if not vs:
                log.warn("Warning: cannot set video region enabled flag on window %i:", wid)
                log.warn(" no video subregion attribute found in %s", type(ws))
                continue
            video_subregions.append(vs)
        #log("_control_video_subregions_from_wid(%s)=%s", wid, video_subregions)
        return video_subregions

    def control_command_video_region_enabled(self, wid, enabled):
        for vs in self._control_video_subregions_from_wid(wid):
            vs.set_enabled(enabled)
        return "video region %s for window %i" % (["disabled", "enabled"][int(enabled)], wid)

    def control_command_video_region_detection(self, wid, detection):
        for vs in self._control_video_subregions_from_wid(wid):
            vs.set_detection(detection)
        return "video region detection %s for window %i" % (["disabled", "enabled"][int(detection)], wid)

    def control_command_video_region(self, wid, x, y, w, h):
        for vs in self._control_video_subregions_from_wid(wid):
            vs.set_region(x, y, w, h)
        return "video region set to %s for window %i" % ((x, y, w, h), wid)

    def control_command_video_region_exclusion_zones(self, wid, zones):
        for vs in self._control_video_subregions_from_wid(wid):
            vs.set_exclusion_zones(zones)
        return "video exclusion zones set to %s for window %i" % (zones, wid)

    def control_command_reset_video_region(self, wid):
        for vs in self._control_video_subregions_from_wid(wid):
            vs.reset()
        return "reset video region heuristics for window %i" % wid


    def control_command_lock_batch_delay(self, wid, delay):
        for ws in self._ws_from_args(wid):
            ws.lock_batch_delay(delay)

    def control_command_unlock_batch_delay(self, wid):
        for ws in self._ws_from_args(wid):
            ws.unlock_batch_delay()

    def control_command_set_lock(self, lock):
        self.lock = parse_bool("lock", lock)
        self.setting_changed("lock", lock is not False)
        self.setting_changed("lock-toggle", lock is None)
        return "lock set to %s" % self.lock

    def control_command_set_sharing(self, sharing):
        self.sharing = parse_bool("sharing", sharing)
        self.setting_changed("sharing", sharing is not False)
        self.setting_changed("sharing-toggle", sharing is None)
        return "sharing set to %s" % self.sharing

    def control_command_set_ui_driver(self, uuid):
        ss = [s for s in self._server_sources.values() if s.uuid==uuid]
        if not ss:
            return "source not found for uuid '%s'" % uuid
        elif len(ss)>1:
            return "more than one source found for uuid '%s'" % uuid
        else:
            self.set_ui_driver(ss)
            return "ui-driver set to %s" % ss

    def control_command_key(self, keycode_str, press = True):
        if self.readonly:
            return
        try:
            if keycode_str.startswith("0x"):
                keycode = int(keycode_str, 16)
            else:
                keycode = int(keycode_str)
        except ValueError:
            raise ControlError("invalid keycode specified: '%s' (not a number)" % keycode_str) from None
        if keycode<=0 or keycode>=255:
            raise ControlError("invalid keycode value: '%s' (must be between 1 and 255)" % keycode_str)
        if press is not True:
            if press in ("1", "press"):
                press = True
            elif press in ("0", "unpress"):
                press = False
            else:
                raise ControlError("if present, the press argument must be one of: %s" %
                                   csv(("1", "press", "0", "unpress")))
        self.fake_key(keycode, press)

    def control_command_sound_output(self, *args):
        msg = []
        for csource in tuple(self._server_sources.values()):
            msg.append("%s : %s" % (csource, csource.sound_control(*args)))
        return csv(msg)

    def control_command_workspace(self, wid, workspace):
        window = self._id_to_window.get(wid)
        if not window:
            raise ControlError("window %s does not exist" % wid)
        if "workspace" not in window.get_property_names():
            raise ControlError("cannot set workspace on window %s" % window)
        if workspace<0:
            raise ControlError("invalid workspace value: %s" % workspace)
        window.set_property("workspace", workspace)
        return "window %s moved to workspace %s" % (wid, workspace)


    def control_command_close(self, wid):
        window = self._id_to_window.get(wid)
        if not window:
            raise ControlError("window %s does not exist" % wid)
        window.request_close()
        return "requested window %s closed" % window

    def control_command_delete(self, wid):
        window = self._id_to_window.get(wid)
        if not window:
            raise ControlError("window %s does not exist" % wid)
        window.send_delete()
        return "requested window %s deleted" % window

    def control_command_move(self, wid, x, y):
        window = self._id_to_window.get(wid)
        if not window:
            raise ControlError("window %s does not exist" % wid)
        ww, wh = window.get_dimensions()
        count = 0
        for source in tuple(self._server_sources.values()):
            move_resize_window = getattr(source, "move_resize_window", None)
            if move_resize_window:
                move_resize_window(wid, window, x, y, ww, wh)
                count += 1
        return "window %s moved to %i,%i for %i clients" % (wid, x, y, count)

    def control_command_resize(self, wid, w, h):
        window = self._id_to_window.get(wid)
        if not window:
            raise ControlError("window %s does not exist" % wid)
        count = 0
        for source in tuple(self._server_sources.values()):
            resize_window = getattr(source, "resize_window", None)
            if resize_window:
                resize_window(wid, window, w, h)
                count += 1
        return "window %s resized to %ix%i for %i clients" % (wid, w, h, count)

    def control_command_moveresize(self, wid, x, y, w, h):
        window = self._id_to_window.get(wid)
        if not window:
            raise ControlError("window %s does not exist" % wid)
        count = 0
        for source in tuple(self._server_sources.values()):
            move_resize_window = getattr(source, "move_resize_window", None)
            if move_resize_window:
                move_resize_window(wid, window, x, y, w, h)
                count += 1
        return "window %s moved to %i,%i and resized to %ix%i for %i clients" % (wid, x, y, w, h, count)


    def _process_command_request(self, _proto, packet):
        """ client sent a command request through its normal channel """
        assert len(packet)>=2, "invalid command request packet (too small!)"
        #packet[0] = "control"
        #this may end up calling do_handle_command_request via the adapter
        code, msg = self.process_control_command(*packet[1:])
        log("command request returned: %s (%s)", code, msg)

    def init_packet_handlers(self):
        self._authenticated_packet_handlers.update({
            "command_request" : self._process_command_request,
          })
