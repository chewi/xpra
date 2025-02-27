# This file is part of Xpra.
# Copyright (C) 2012-2019 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""
XSETTINGS

This code deals with:
* extracting data from XSETTINGS into nice python data structures
and
* converting those structures back into XSETTINGS format

It is used by xpra.x11.gtk_x11.prop
"""

import os
import sys
import struct

from xpra.log import Logger
from xpra.util import envbool
from xpra.os_util import strtobytes, bytestostr, hexstr


log = Logger("x11", "xsettings")

BLACKLISTED_XSETTINGS = os.environ.get("XPRA_BLACKLISTED_XSETTINGS",
                                       "Gdk/WindowScalingFactor,Gtk/SessionBusId,Gtk/IMModule").split(",")


#undocumented XSETTINGS endianess values:
LITTLE_ENDIAN = 0
BIG_ENDIAN    = 1
def get_local_byteorder():
    if sys.byteorder=="little":
        return LITTLE_ENDIAN
    return BIG_ENDIAN   # pragma: no cover

#the 3 types of settings supported:
XSettingsTypeInteger = 0
XSettingsTypeString = 1
XSettingsTypeColor = 2

XSettingsNames = {
                XSettingsTypeInteger    : "Integer",
                XSettingsTypeString     : "String",
                XSettingsTypeColor      : "Color",
                }


XSETTINGS_CACHE = {}
def get_settings(d):
    global XSETTINGS_CACHE
    DEBUG_XSETTINGS = envbool("XPRA_XSETTINGS_DEBUG", False)
    #parse xsettings according to
    #http://standards.freedesktop.org/xsettings-spec/xsettings-spec-0.5.html
    assert len(d)>=12, "_XSETTINGS_SETTINGS property is too small: %s" % len(d)
    if DEBUG_XSETTINGS:
        log("get_settings(%s)", tuple(d))
    byte_order, _, _, _, serial, n_settings = struct.unpack(b"=BBBBII", d[:12])
    cache = XSETTINGS_CACHE
    log("get_settings(..) found byte_order=%s (local is %s), serial=%s, n_settings=%s, cache=%s",
        byte_order, get_local_byteorder(), serial, n_settings, cache)
    if cache and cache[0]==serial:
        log("get_settings(..) returning value from cache")
        return cache
    settings = []
    pos = 12
    while n_settings>len(settings):
        log("get_settings(..) pos=%i (len=%i), data=%s", pos, len(d), hexstr(d[pos:]))
        istart = pos
        #parse header:
        setting_type, _, name_len = struct.unpack(b"=BBH", d[pos:pos+4])
        pos += 4
        #extract property name:
        prop_name = d[pos:pos+name_len]
        pos += (name_len + 0x3) & ~0x3
        #serial:
        assert len(d)>=pos+4, "not enough data (%s bytes) to extract serial (4 bytes needed)" % (len(d)-pos)
        last_change_serial = struct.unpack(b"=I", d[pos:pos+4])[0]
        pos += 4
        if DEBUG_XSETTINGS:
            log("get_settings(..) found property %s of type %s, serial=%s",
                prop_name, XSettingsNames.get(setting_type, "INVALID!"), last_change_serial)
        #extract value:
        if setting_type==XSettingsTypeInteger:
            assert len(d)>=pos+4, "not enough data (%s bytes) to extract int (4 bytes needed)" % (len(d)-pos)
            value = int(struct.unpack(b"=I", d[pos:pos+4])[0])
            pos += 4
        elif setting_type==XSettingsTypeString:
            assert len(d)>=pos+4, "not enough data (%s bytes) to extract string length (4 bytes needed)" % (len(d)-pos)
            value_len = struct.unpack(b"=I", d[pos:pos+4])[0]
            assert len(d)>=pos+4+value_len, "not enough data (%s bytes) to extract string (%s bytes needed)" % (len(d)-pos-4, value_len)
            value = d[pos+4:pos+4+value_len]
            pos += 4 + ((value_len + 0x3) & ~0x3)
        elif setting_type==XSettingsTypeColor:
            assert len(d)>=pos+8, "not enough data (%s bytes) to extract color (8 bytes needed)" % (len(d)-pos)
            red, blue, green, alpha = struct.unpack(b"=HHHH", d[pos:pos+8])
            value = (red, blue, green, alpha)
            pos += 8
        else:
            log.error("invalid setting type: %s, cannot continue parsing XSETTINGS!", setting_type)
            break
        setting = setting_type, prop_name, value, last_change_serial
        if DEBUG_XSETTINGS:
            log("get_settings(..) %s -> %s", tuple(d[istart:pos]), setting)
        settings.append(setting)
    log("get_settings(..) settings=%s", settings)
    XSETTINGS_CACHE = (serial, settings)
    return  serial, settings

def set_settings(d):
    assert len(d)==2, "invalid format for XSETTINGS: %s" % str(d)
    serial, settings = d
    log("set_settings(%s) serial=%s, %s settings", d, serial, len(settings))
    all_bin_settings = []
    for setting in settings:
        setting_type, prop_name, value, last_change_serial = setting
        prop_name = strtobytes(prop_name)
        try:
            log("set_settings(..) processing property %s of type %s",
                bytestostr(prop_name), XSettingsNames.get(setting_type, "INVALID!"))
            x = struct.pack(b"=BBH", setting_type, 0, len(prop_name))
            x += prop_name
            pad_len = ((len(prop_name) + 0x3) & ~0x3) - len(prop_name)
            x += b'\0'*pad_len
            x += struct.pack(b"=I", last_change_serial)
            if setting_type==XSettingsTypeInteger:
                assert isinstance(value, int), f"invalid value type: integer wanted, not {type(value)}"
                x += struct.pack(b"=I", int(value))
            elif setting_type==XSettingsTypeString:
                value = strtobytes(value)
                x += struct.pack(b"=I", len(value))
                x += value
                pad_len = ((len(value) + 0x3) & ~0x3) - len(value)
                x += b'\0'*pad_len
            elif setting_type==XSettingsTypeColor:
                red, blue, green, alpha = value
                x += struct.pack(b"=HHHH", red, blue, green, alpha)
            else:
                log.error("Error: invalid type %i for xsetting property '%s'", setting_type, bytestostr(prop_name))
                continue
            log("set_settings(..) %s -> %s", setting, tuple(x))
            all_bin_settings.append(x)
        except Exception as e:
            log("set_settings(%s)", d, exc_info=True)
            log.error("Error processing XSettings property %s:", bytestostr(prop_name))
            log.error(" type=%s, value=%s", XSettingsNames.get(setting_type, "INVALID!"), value)
            log.error(" %s", e)
    #header
    v = struct.pack(b"=BBBBII", get_local_byteorder(), 0, 0, 0, serial, len(all_bin_settings))
    v += b"".join(all_bin_settings)  #values
    v += b'\0'                       #null terminated
    log("set_settings(%s)=%s", d, tuple(v))
    return  v


def main(): # pragma: no cover
    from xpra.platform.gui import init as gui_init
    from xpra.os_util import POSIX
    from xpra.platform import program_context
    from xpra.gtk_common.error import xsync
    with program_context("XSettings"):
        gui_init()
        verbose = "-v" in sys.argv or "--verbose" in sys.argv
        if verbose:
            from xpra.log import get_all_loggers
            for x in get_all_loggers():
                x.enable_debug()

        #naughty, but how else can I hook this up?
        if not POSIX:
            print("xsettings require a posix OS")
            return 1

        with xsync:
            from xpra.x11.bindings.posix_display_source import init_posix_display_source    #@UnresolvedImport
            init_posix_display_source()
            from xpra.x11.bindings.window_bindings import X11WindowBindings #@UnresolvedImport
            window_bindings = X11WindowBindings()
            selection = "_XSETTINGS_S0"
            owner = window_bindings.XGetSelectionOwner(selection)
            print("owner(%s)=%#x" % (selection, owner))
            XSETTINGS = "_XSETTINGS_SETTINGS"
            if owner:
                data = window_bindings.XGetWindowProperty(owner, XSETTINGS, XSETTINGS)
                serial, settings = get_settings(data)
                print("serial=%s" % serial)
                print("%s settings:" % len(settings))
                for s in settings:
                    print(s)
            return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
