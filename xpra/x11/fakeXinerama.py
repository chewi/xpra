#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2014-2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os

from xpra.util import prettify_plug_name
from xpra.os_util import find_lib, find_lib_ldconfig, LINUX, POSIX
from xpra.version_util import XPRA_VERSION
from xpra.log import Logger

log = Logger("x11", "server", "util")

fakeXinerama_config_files = [
            #the new fakexinerama file:
            os.path.expanduser("~/.%s-fakexinerama" % os.environ.get("DISPLAY")),
            #compat file for "old" version found on github:
            os.path.expanduser("~/.fakexinerama"),
           ]

def find_libfakeXinerama():
    libname = "fakeXinerama"
    try:
        from ctypes.util import find_library
        flibname = find_library("fakeXinerama")
        if flibname:
            libname = flibname
    except Exception:
        pass
    if POSIX:
        for lib_dir in os.environ.get("LD_LIBRARY_PATH", "/usr/lib").split(os.pathsep):
            lib_path = os.path.join(lib_dir, libname)
            if not os.path.exists(lib_dir):
                continue
            if os.path.exists(lib_path) and os.path.isfile(lib_path):
                return lib_path
    if LINUX:
        try:
            libpath = find_lib_ldconfig("fakeXinerama")
            if libpath:
                return libpath
        except Exception as e:
            log("find_libfakeXinerama()", exc_info=True)
            log.error("Error: cannot launch ldconfig -p to locate libfakeXinerama:")
            log.error(" %s", e)
    return find_lib("libfakeXinerama.so.1")

current_xinerama_config = None

def save_fakeXinerama_config(supported=True, source="", ss=()):
    """ returns True if the fakexinerama config was modified """
    global current_xinerama_config
    def delfile(msg):
        global current_xinerama_config
        if msg:
            log.warn(msg)
        cleanup_fakeXinerama()
        oldconf = current_xinerama_config
        current_xinerama_config = None
        return oldconf is not None
    if not supported:
        return delfile(None)
    if not ss:
        return delfile("cannot save fake xinerama settings: no display found")
    if len(ss)>1:
        return delfile("cannot save fake xinerama settings: more than one display found")
    display_info = ss[0]
    if len(display_info)==2 and isinstance(display_info[0], int) and isinstance(display_info[1], int):
        #just WxH, not enough display information
        return delfile("cannot save fake xinerama settings: missing display data from client %s" % source)
    if len(display_info)<10:
        return delfile("cannot save fake xinerama settings: incomplete display data from client %s" % source)
    #display_name, width, height, width_mm, height_mm, \
    #monitors, work_x, work_y, work_width, work_height = s[:11]
    monitors = display_info[5]
    if len(monitors)==0:
        return delfile("cannot save fake xinerama settings: no monitors!")
    if len(monitors)>=10:
        return delfile("cannot save fake xinerama settings: too many monitors! (%s)" % len(monitors))
    #generate the file data:
    data = ["# file generated by xpra %s for display %s" % (XPRA_VERSION, os.environ.get("DISPLAY")),
            "# %s monitors:" % len(monitors),
            "%s" % len(monitors)]
    #the new config (numeric values only)
    config = [len(monitors)]
    for i, m in enumerate(monitors):
        if len(m)<7:
            return delfile("cannot save fake xinerama settings: incomplete monitor data for monitor: %s" % (m, ))
        plug_name, x, y, width, height, wmm, hmm = m[:7]
        data.append("# %s (%smm x %smm)" % (prettify_plug_name(plug_name, "monitor %s" % i), wmm, hmm))
        data.append("%s %s %s %s" % (x, y, width, height))
        config.append((x, y, width, height))
    if current_xinerama_config==config:
        #we assume that no other process is going to overwrite the deprecated .fakexinerama
        log("fake xinerama config unchanged")
        return False
    log("fake xinerama config changed:")
    log(" old=%s", current_xinerama_config)
    log(" new=%s", config)
    current_xinerama_config = config
    data.append("")
    contents = "\n".join(data)
    for filename in fakeXinerama_config_files:
        try:
            with open(filename, "w", encoding="utf8") as f:
                f.write(contents)
        except Exception as e:
            log("writing to '%s'", filename, exc_info=True)
            log.warn("Error writing fake xinerama file '%s':", filename)
            log.warn(" %s", e)
    log("saved %s monitors to fake xinerama files: %s", len(monitors), fakeXinerama_config_files)
    return True

def cleanup_fakeXinerama():
    log("cleanup_fakeXinerama() configs=%s", fakeXinerama_config_files)
    for f in fakeXinerama_config_files:
        try:
            if os.path.exists(f):
                log("cleanup_fakexinerama() deleting fake xinerama file '%s'", f)
                os.unlink(f)
        except Exception as e:
            log.error("Error: failed to delete fakexinerama config file")
            log.error(" '%s': %s", f, e)


def main():
    print("libfakeXinerama=%s" % find_libfakeXinerama())

if __name__ == "__main__":
    main()
