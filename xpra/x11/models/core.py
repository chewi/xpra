# This file is part of Xpra.
# Copyright (C) 2008, 2009 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import signal
from socket import gethostname
from gi.repository import GObject, Gdk, GLib

from xpra.util import envbool, first_time
from xpra.os_util import bytestostr, get_proc_cmdline
from xpra.x11.common import Unmanageable
from xpra.gtk_common.gobject_util import one_arg_signal, two_arg_signal
from xpra.gtk_common.error import XError, xsync, xswallow
from xpra.x11.bindings.window_bindings import X11WindowBindings, constants, SHAPE_KIND #@UnresolvedImport
from xpra.x11.bindings.res_bindings import ResBindings #@UnresolvedImport
from xpra.x11.models.model_stub import WindowModelStub
from xpra.x11.gtk_x11.composite import CompositeHelper
from xpra.x11.gtk_x11.prop import prop_get, prop_set, prop_type_get, PYTHON_TYPES
from xpra.x11.gtk_x11.send_wm import send_wm_delete_window
from xpra.x11.gtk_x11.gdk_bindings import add_event_receiver, remove_event_receiver
from xpra.log import Logger

log = Logger("x11", "window")
metalog = Logger("x11", "window", "metadata")
shapelog = Logger("x11", "window", "shape")
grablog = Logger("x11", "window", "grab")
framelog = Logger("x11", "window", "frame")
geomlog = Logger("x11", "window", "geometry")


X11Window = X11WindowBindings()
em = Gdk.EventMask
ADDMASK = em.STRUCTURE_MASK | em.PROPERTY_CHANGE_MASK | em.FOCUS_CHANGE_MASK | em.POINTER_MOTION_MASK

XRes = ResBindings()
if not XRes.check_xres():
    log.warn("Warning: X Resource Extension missing or too old")
    XRes = None

try:
    from xpra.platform.xposix.proc import get_parent_pid
except ImportError:
    log("proc.get_parent_pid is not available", exc_info=True)
    get_parent_pid = None

FORCE_QUIT = envbool("XPRA_FORCE_QUIT", True)
XSHAPE = envbool("XPRA_XSHAPE", True)
FRAME_EXTENTS = envbool("XPRA_FRAME_EXTENTS", True)
OPAQUE_REGION = envbool("XPRA_OPAQUE_REGION", True)

# Re-stacking:
Above = 0
Below = 1
TopIf = 2
BottomIf = 3
Opposite = 4
RESTACKING_STR = {
    Above : "Above",
    Below : "Below",
    TopIf : "TopIf",
    BottomIf : "BottomIf",
    Opposite : "Opposite",
    }

# grab stuff:
NotifyNormal        = constants["NotifyNormal"]
NotifyGrab          = constants["NotifyGrab"]
NotifyUngrab        = constants["NotifyUngrab"]
NotifyWhileGrabbed  = constants["NotifyWhileGrabbed"]
NotifyNonlinearVirtual = constants["NotifyNonlinearVirtual"]
GRAB_CONSTANTS = {
                  NotifyNormal          : "NotifyNormal",
                  NotifyGrab            : "NotifyGrab",
                  NotifyUngrab          : "NotifyUngrab",
                  NotifyWhileGrabbed    : "NotifyWhileGrabbed",
                 }
DETAIL_CONSTANTS    = {}
for dconst in (
    "NotifyAncestor", "NotifyVirtual", "NotifyInferior",
    "NotifyNonlinear", "NotifyNonlinearVirtual", "NotifyPointer",
    "NotifyPointerRoot", "NotifyDetailNone",
    ):
    DETAIL_CONSTANTS[constants[dconst]] = dconst
grablog("pointer grab constants: %s", GRAB_CONSTANTS)
grablog("detail constants: %s", DETAIL_CONSTANTS)

#these properties are not handled, and we don't want to spam the log file
#whenever an app decides to change them:
PROPERTIES_IGNORED = [x for x in os.environ.get("XPRA_X11_PROPERTIES_IGNORED", "").split(",") if x]
#make it easier to debug property changes, just add them here:
#ie: {"WM_PROTOCOLS" : ["atom"]}
X11_PROPERTIES_DEBUG = {}
PROPERTIES_DEBUG = [prop_debug.strip()
                    for prop_debug in os.environ.get("XPRA_WINDOW_PROPERTIES_DEBUG", "").split(",")]
X11PROPERTY_SYNC = envbool("XPRA_X11PROPERTY_SYNC", True)
X11PROPERTY_SYNC_BLACKLIST = os.environ.get("XPRA_X11PROPERTY_SYNC_BLACKLIST",
                                            "_GTK,WM_,_NET,Xdnd").split(",")


def sanestr(s):
    return (s or "").strip("\0").replace("\0", " ")


class CoreX11WindowModel(WindowModelStub):
    """
        The utility superclass for all GTK2 / X11 window models,
        it wraps an X11 window (the "client-window").
        Defines the common properties and signals,
        sets up the composite helper so we get the damage events.
        The x11_property_handlers sync X11 window properties into Python objects,
        the py_property_handlers do it in the other direction.
    """
    __common_properties__ = {
        #the actual X11 client window
        "client-window": (GObject.TYPE_PYOBJECT,
                "gtk.gdk.Window representing the client toplevel", "",
                GObject.ParamFlags.READABLE),
        #the X11 window id
        "xid": (GObject.TYPE_INT,
                "X11 window id", "",
                -1, 65535, -1,
                GObject.ParamFlags.READABLE),
        #FIXME: this is an ugly virtual property
        "geometry": (GObject.TYPE_PYOBJECT,
                "current coordinates (x, y, w, h, border) for the window", "",
                GObject.ParamFlags.READABLE),
        #bits per pixel
        "depth": (GObject.TYPE_INT,
                "window bit depth", "",
                -1, 64, -1,
                GObject.ParamFlags.READABLE),
        #if the window depth is 32 bit
        "has-alpha": (GObject.TYPE_BOOLEAN,
                "Does the window use transparency", "",
                False,
                GObject.ParamFlags.READABLE),
        #from WM_CLIENT_MACHINE
        "client-machine": (GObject.TYPE_PYOBJECT,
                "Host where client process is running", "",
                GObject.ParamFlags.READABLE),
        #from XResGetClientPid
        "pid": (GObject.TYPE_INT,
                "PID of owning process", "",
                -1, 65535, -1,
                GObject.ParamFlags.READABLE),
        "ppid": (GObject.TYPE_INT,
                "PID of parent process", "",
                -1, 65535, -1,
                GObject.ParamFlags.READABLE),
        #from _NET_WM_PID
        "wm-pid": (GObject.TYPE_INT,
                "PID of owning process", "",
                -1, 65535, -1,
                GObject.ParamFlags.READABLE),
        #from _NET_WM_NAME or WM_NAME
        "title": (GObject.TYPE_PYOBJECT,
                "Window title (unicode or None)", "",
                GObject.ParamFlags.READABLE),
        #from WM_WINDOW_ROLE
        "role" : (GObject.TYPE_PYOBJECT,
                "The window's role (ICCCM session management)", "",
                GObject.ParamFlags.READABLE),
        #from WM_PROTOCOLS via XGetWMProtocols
        "protocols": (GObject.TYPE_PYOBJECT,
                "Supported WM protocols", "",
                GObject.ParamFlags.READABLE),
        #from WM_COMMAND
        "command": (GObject.TYPE_PYOBJECT,
                "Command used to start or restart the client", "",
                GObject.ParamFlags.READABLE),
        #from WM_CLASS via getClassHint
        "class-instance": (GObject.TYPE_PYOBJECT,
                "Classic X 'class' and 'instance'", "",
                GObject.ParamFlags.READABLE),
        #ShapeNotify events will populate this using XShapeQueryExtents
        "shape": (GObject.TYPE_PYOBJECT,
                "Window XShape data", "",
                GObject.ParamFlags.READABLE),
        #synced to "_NET_FRAME_EXTENTS"
        "frame": (GObject.TYPE_PYOBJECT,
                "Size of the window frame, as per _NET_FRAME_EXTENTS", "",
                GObject.ParamFlags.READWRITE),
        #synced to "_NET_WM_ALLOWED_ACTIONS"
        "allowed-actions": (GObject.TYPE_PYOBJECT,
                "Supported WM actions", "",
                GObject.ParamFlags.READWRITE),
        #synced to "_NET_WM_OPAQUE_REGION"
        "opaque-region": (GObject.TYPE_PYOBJECT,
                "Compositor can assume that there is no transparency for this region", "",
                GObject.ParamFlags.READWRITE),
           }

    __common_signals__ = {
        #signals we emit:
        "unmanaged"                     : one_arg_signal,
        "restack"                       : two_arg_signal,
        "initiate-moveresize"           : one_arg_signal,
        "grab"                          : one_arg_signal,
        "ungrab"                        : one_arg_signal,
        "bell"                          : one_arg_signal,
        "client-contents-changed"       : one_arg_signal,
        "motion"                        : one_arg_signal,
        #x11 events we catch (and often re-emit as something else):
        "xpra-property-notify-event"    : one_arg_signal,
        "xpra-xkb-event"                : one_arg_signal,
        "xpra-shape-event"              : one_arg_signal,
        "xpra-configure-event"          : one_arg_signal,
        "xpra-unmap-event"              : one_arg_signal,
        "xpra-client-message-event"     : one_arg_signal,
        "xpra-focus-in-event"           : one_arg_signal,
        "xpra-focus-out-event"          : one_arg_signal,
        "xpra-motion-event"             : one_arg_signal,
        "x11-property-changed"          : one_arg_signal,
        }

    #things that we expose:
    _property_names         = [
        "xid", "depth", "has-alpha",
        "client-machine", "pid", "ppid", "wm-pid",
        "title", "role",
        "command", "shape",
        "class-instance", "protocols",
        "opaque-region",
        ]
    #exposed and changing (should be watched for notify signals):
    _dynamic_property_names = ["title", "command", "shape", "class-instance", "protocols", "opaque-region"]
    #should not be exported to the clients:
    _internal_property_names = ["frame", "allowed-actions"]
    _initial_x11_properties = ["_NET_WM_PID", "WM_CLIENT_MACHINE",
                               #_NET_WM_NAME is redundant, as it calls the same handler as "WM_NAME"
                               "WM_NAME", "_NET_WM_NAME",
                               "WM_PROTOCOLS", "WM_CLASS", "WM_WINDOW_ROLE",
                               "_NET_WM_OPAQUE_REGION",
                               "WM_COMMAND",
                               ]
    _DEFAULT_NET_WM_ALLOWED_ACTIONS = []
    _MODELTYPE = "Core"
    _scrub_x11_properties       = [
                              "WM_STATE",
                              #"_NET_WM_STATE",    # "..it should leave the property in place when it is shutting down"
                              "_NET_FRAME_EXTENTS", "_NET_WM_ALLOWED_ACTIONS"]

    def __init__(self, client_window):
        super().__init__()
        self.xid = client_window.get_xid()
        log("new window %#x", self.xid)
        self.client_window = client_window
        self.client_window_saved_events = self.client_window.get_events()
        self._composite = None
        self._damage_forward_handle = None
        self._setup_done = False
        self._kill_count = 0
        self._internal_set_property("client-window", client_window)


    def __repr__(self):  #pylint: disable=arguments-differ
        try:
            return "%s(%#x)" % (type(self).__name__, self.xid)
        except AttributeError:
            return repr(self)


    #########################################
    # Setup and teardown
    #########################################

    def call_setup(self):
        """
            Call this method to prepare the window:
            * makes sure it still exists
              (by querying its geometry which may raise an XError)
            * setup composite redirection
            * calls setup
            The difficulty comes from X11 errors and synchronization:
            we want to catch errors and undo what we've done.
            The mix of GTK and pure-X11 calls is not helping.
        """
        try:
            with xsync:
                geom = X11Window.geometry_with_border(self.xid)
                if geom is None:
                    raise Unmanageable("window %#x disappeared already" % self.xid)
                self._internal_set_property("geometry", geom[:4])
                self._read_initial_X11_properties()
        except XError as e:
            log("failed to manage %#x", self.xid, exc_info=True)
            raise Unmanageable(e) from e
        add_event_receiver(self.client_window, self)
        # Keith Packard says that composite state is undefined following a
        # reparent, so I'm not sure doing this here in the superclass,
        # before we reparent, actually works... let's wait and see.
        try:
            self._composite = CompositeHelper(self.client_window)
            with xsync:
                self._composite.setup()
                if X11Window.displayHasXShape():
                    X11Window.XShapeSelectInput(self.xid)
        except Exception as e:
            remove_event_receiver(self.client_window, self)
            log("%s %#x does not support compositing: %s", self._MODELTYPE, self.xid, e)
            with xswallow:
                self._composite.destroy()
            self._composite = None
            if isinstance(e, Unmanageable):
                raise
            raise Unmanageable(e) from e
        #compositing is now enabled,
        #from now on we must call setup_failed to clean things up
        self._managed = True
        try:
            with xsync:
                self.setup()
        except XError as e:
            log("failed to setup %#x", self.xid, exc_info=True)
            try:
                with xsync:
                    self.setup_failed(e)
            except Exception as ex:
                log.error("error in cleanup handler: %s", ex)
            raise Unmanageable(e) from None
        self._setup_done = True

    def setup_failed(self, e):
        log("cannot manage %s %#x: %s", self._MODELTYPE, self.xid, e)
        self.do_unmanaged(False)

    def setup(self):
        # Start listening for important events.
        X11Window.addDefaultEvents(self.xid)
        self._damage_forward_handle = self._composite.connect("contents-changed", self._forward_contents_changed)
        self._setup_property_sync()


    def unmanage(self, exiting=False):
        if self._managed:
            self.emit("unmanaged", exiting)

    def do_unmanaged(self, wm_exiting):
        if not self._managed:
            return
        self._managed = False
        log("%s.do_unmanaged(%s) damage_forward_handle=%s, composite=%s",
            self._MODELTYPE, wm_exiting, self._damage_forward_handle, self._composite)
        remove_event_receiver(self.client_window, self)
        GLib.idle_add(self.managed_disconnect)
        if self._composite:
            if self._damage_forward_handle:
                self._composite.disconnect(self._damage_forward_handle)
                self._damage_forward_handle = None
            self._composite.destroy()
            self._composite = None
            self._scrub_x11()


    #########################################
    # Damage / Composite
    #########################################

    def acknowledge_changes(self):
        c = self._composite
        assert c, "composite window destroyed outside the UI thread?"
        c.acknowledge_changes()

    def _forward_contents_changed(self, _obj, event):
        if self._managed:
            self.emit("client-contents-changed", event)

    def uses_XShm(self) -> bool:
        c = self._composite
        return c and c.has_xshm()

    def get_image(self, x, y, width, height):
        return self._composite.get_image(x, y, width, height)


    def _setup_property_sync(self):
        metalog("setup_property_sync()")
        #python properties which trigger an X11 property to be updated:
        for prop, cb in self._py_property_handlers.items():
            self.connect("notify::%s" % prop, cb)
        #initial sync:
        for cb in self._py_property_handlers.values():
            cb(self)
        #this one is special, and overriden in BaseWindow too:
        self.managed_connect("notify::protocols", self._update_can_focus)

    def _update_can_focus(self, *_args):
        can_focus = "WM_TAKE_FOCUS" in self.get_property("protocols")
        self._updateprop("can-focus", can_focus)

    def _read_initial_X11_properties(self):
        """ This is called within an XSync context,
            so that X11 calls can raise XErrors,
            pure GTK calls are not allowed. (they would trap the X11 error and crash!)
            Calling _updateprop is safe, because setup has not completed yet,
            so the property update will not fire notify()
        """
        metalog("read_initial_X11_properties() core")
        #immutable ones:
        depth = X11Window.get_depth(self.xid)
        pid = XRes.get_pid(self.xid) if XRes else -1
        ppid = get_parent_pid(pid) if pid and get_parent_pid else 0
        metalog("initial X11 properties: xid=%#x, depth=%i, pid=%i, ppid=%i", self.xid, depth, pid, ppid)
        self._updateprop("depth", depth)
        self._updateprop("xid", self.xid)
        self._updateprop("pid", pid)
        self._updateprop("ppid", ppid)
        self._updateprop("has-alpha", depth==32)
        self._updateprop("allowed-actions", self._DEFAULT_NET_WM_ALLOWED_ACTIONS)
        self._updateprop("shape", self._read_xshape())
        #note: some of those are technically mutable,
        #but we don't export them as "dynamic" properties, so this won't be propagated
        #maybe we want to catch errors parsing _NET_WM_ICON ?
        metalog("initial X11_properties: querying %s", self._initial_x11_properties)
        #to make sure we don't call the same handler twice which is pointless
        #(the same handler may handle more than one X11 property)
        handlers = set()
        for mutable in self._initial_x11_properties:
            handler = self._x11_property_handlers.get(mutable)
            if not handler:
                log.error("BUG: unknown initial X11 property: %s", mutable)
            elif handler not in handlers:
                handlers.add(handler)
                try:
                    handler(self)
                except XError:
                    log("handler %s failed", handler, exc_info=True)
                    #these will be caught in call_setup()
                    raise
                except Exception:
                    #try to continue:
                    log.error("Error parsing initial property '%s':", mutable, exc_info=True)

    def _scrub_x11(self):
        metalog("scrub_x11() x11 properties=%s", self._scrub_x11_properties)
        if not self._scrub_x11_properties:
            return
        with xswallow:
            for prop in self._scrub_x11_properties:
                X11Window.XDeleteProperty(self.xid, prop)


    #########################################
    # XShape
    #########################################

    def _read_xshape(self, x=0, y=0):
        if not X11Window.displayHasXShape() or not XSHAPE:
            return {}
        extents = X11Window.XShapeQueryExtents(self.xid)
        if not extents:
            shapelog("read_shape for window %#x: no extents", self.xid)
            return {}
        #w,h = X11Window.getGeometry(xid)[2:4]
        shapelog("read_shape for window %#x: extents=%s", self.xid, extents)
        bextents = extents[0]
        cextents = extents[1]
        if bextents[0]==0 and cextents[0]==0:
            shapelog("read_shape for window %#x: none enabled", self.xid)
            return {}
        v = {
             "x"                : x,
             "y"                : y,
             "Bounding.extents" : bextents,
             "Clip.extents"     : cextents,
             }
        for kind, kind_name in SHAPE_KIND.items():  # @UndefinedVariable
            rectangles = X11Window.XShapeGetRectangles(self.xid, kind)
            v[kind_name+".rectangles"] = rectangles
        shapelog("_read_shape()=%s", v)
        return v


    ################################
    # Property reading
    ################################

    def get_dimensions(self):
        #just extracts the size from the geometry:
        return self.get_property("geometry")[2:4]

    def get_geometry(self):
        return self.get_property("geometry")[:4]


    #########################################
    # Python objects synced to X11 properties
    #########################################

    def prop_set(self, key, ptype, value):
        prop_set(self.client_window, key, ptype, value)


    def _sync_allowed_actions(self, *_args):
        actions = self.get_property("allowed-actions") or []
        metalog("sync_allowed_actions: setting _NET_WM_ALLOWED_ACTIONS=%s on %#x", actions, self.xid)
        with xswallow:
            prop_set(self.client_window, "_NET_WM_ALLOWED_ACTIONS", ["atom"], actions)
    def _handle_frame_changed(self, *_args):
        #legacy name for _sync_frame() called from Wm
        self._sync_frame()
    def _sync_frame(self, *_args):
        if not FRAME_EXTENTS:
            return
        v = self.get_property("frame")
        framelog("sync_frame: frame(%#x)=%s", self.xid, v)
        if not v and (not self.is_OR() and not self.is_tray()):
            root = self.client_window.get_screen().get_root_window()
            v = prop_get(root, "DEFAULT_NET_FRAME_EXTENTS", ["u32"], ignore_errors=True)
        if not v:
            #default for OR, or if we don't have any other value:
            v = (0, 0, 0, 0)
        framelog("sync_frame: setting _NET_FRAME_EXTENTS=%s on %#x", v, self.xid)
        with xswallow:
            prop_set(self.client_window, "_NET_FRAME_EXTENTS", ["u32"], v)

    _py_property_handlers = {
        "allowed-actions"    : _sync_allowed_actions,
        "frame"              : _sync_frame,
        }


    #########################################
    # X11 properties synced to Python objects
    #########################################

    def prop_get(self, key, ptype, ignore_errors=None, raise_xerrors=False):
        """
            Get an X11 property from the client window,
            using the automatic type conversion code from prop.py
            Ignores property errors during setup_client.
        """
        if ignore_errors is None and (not self._setup_done or not self._managed):
            ignore_errors = True
        return prop_get(self.client_window, key, ptype, ignore_errors=bool(ignore_errors), raise_xerrors=raise_xerrors)


    def do_xpra_property_notify_event(self, event):
        #X11: PropertyNotify
        assert event.window is self.client_window
        self._handle_property_change(str(event.atom))

    def _handle_property_change(self, name):
        #ie: _handle_property_change("_NET_WM_NAME")
        metalog("Property changed on %#x: %s", self.xid, name)
        x11proptype = X11_PROPERTIES_DEBUG.get(name)
        if x11proptype is not None:
            metalog.info("%s=%s", name, self.prop_get(name, x11proptype, True, False))
        if name in PROPERTIES_IGNORED:
            return
        if X11PROPERTY_SYNC and not any (name.startswith(x) for x in X11PROPERTY_SYNC_BLACKLIST):
            try:
                with xsync:
                    prop_type = prop_type_get(self.client_window, name)
                    metalog("_handle_property_change(%s) property type=%s", name, prop_type)
                    if prop_type:
                        dtype, dformat = prop_type
                        ptype = PYTHON_TYPES.get(bytestostr(dtype))
                        if ptype:
                            value = self.prop_get(name, ptype, ignore_errors=True)
                            if value is None:
                                #retry using scalar type:
                                value = self.prop_get(name, (ptype,), ignore_errors=True)
                            metalog("_handle_property_change(%s) value=%s", name, value)
                            if value:
                                self.emit("x11-property-changed", (name, ptype, dformat, value))
                                return
            except Exception:
                metalog("_handle_property_change(%s)", name, exc_info=True)
            self.emit("x11-property-changed", (name, "", 0, ""))
        handler = self._x11_property_handlers.get(name)
        if handler:
            try:
                with xsync:
                    handler(self)
            except XError as e:
                log("_handle_property_change", exc_info=True)
                log.error("Error processing property change for '%s'", name)
                log.error(" on window %#x", self.xid)
                log.error(" %s", e)

    #specific properties:
    def _handle_pid_change(self):
        pid = self.prop_get("_NET_WM_PID", "u32") or -1
        metalog("_NET_WM_PID=%s", pid)
        self._updateprop("wm-pid", pid)

    def _handle_client_machine_change(self):
        client_machine = self.prop_get("WM_CLIENT_MACHINE", "latin1")
        metalog("WM_CLIENT_MACHINE=%s", client_machine)
        self._updateprop("client-machine", client_machine)

    def _handle_wm_name_change(self):
        name = self.prop_get("_NET_WM_NAME", "utf8", True)
        metalog("_NET_WM_NAME=%s", name)
        if name is None:
            name = self.prop_get("WM_NAME", "latin1", True)
            metalog("WM_NAME=%s", name)
        if self._updateprop("title", sanestr(name)):
            metalog("wm_name changed")

    def _handle_role_change(self):
        role = self.prop_get("WM_WINDOW_ROLE", "latin1")
        metalog("WM_WINDOW_ROLE=%s", role)
        self._updateprop("role", role)

    def _handle_protocols_change(self):
        with xsync:
            protocols = X11Window.XGetWMProtocols(self.xid)
        metalog("WM_PROTOCOLS=%s", protocols)
        self._updateprop("protocols", protocols)

    def _handle_command_change(self):
        command = self.prop_get("WM_COMMAND", "latin1")
        metalog("WM_COMMAND=%s", command)
        if command:
            command = command.strip("\0")
        else:
            pid = self.get_property("pid")
            command = b" ".join(get_proc_cmdline(pid) or ())
        self._updateprop("command", command)

    def _handle_class_change(self):
        class_instance = X11Window.getClassHint(self.xid)
        if class_instance:
            class_instance = tuple(v.decode("latin1") for v in class_instance)
        metalog("WM_CLASS=%s", class_instance)
        self._updateprop("class-instance", class_instance)

    def _handle_opaque_region_change(self):
        rectangles = []
        v = tuple(self.prop_get("_NET_WM_OPAQUE_REGION", ["u32"]) or [])
        if OPAQUE_REGION and len(v)%4==0:
            while v:
                rectangles.append(v[:4])
                v = v[4:]
        metalog("_NET_WM_OPAQUE_REGION(%s)=%s (OPAQUE_REGION=%s)", v, rectangles, OPAQUE_REGION)
        self._updateprop("opaque-region", tuple(rectangles))

    #these handlers must not generate X11 errors (must use XSync)
    _x11_property_handlers = {
        "_NET_WM_PID"       : _handle_pid_change,
        "WM_CLIENT_MACHINE" : _handle_client_machine_change,
        "WM_NAME"           : _handle_wm_name_change,
        "_NET_WM_NAME"      : _handle_wm_name_change,
        "WM_WINDOW_ROLE"    : _handle_role_change,
        "WM_PROTOCOLS"      : _handle_protocols_change,
        "WM_COMMAND"        : _handle_command_change,
        "WM_CLASS"          : _handle_class_change,
        "_NET_WM_OPAQUE_REGION" : _handle_opaque_region_change,
        }


    #########################################
    # X11 Events
    #########################################

    def do_xpra_unmap_event(self, _event):
        self.unmanage()

    def do_xpra_destroy_event(self, event):
        if event.delivered_to is self.client_window:
            # This is somewhat redundant with the unmap signal, because if you
            # destroy a mapped window, then a UnmapNotify is always generated.
            # However, this allows us to catch the destruction of unmapped
            # ("iconified") windows, and also catch any mistakes we might have
            # made with unmap heuristics.  I love the smell of XDestroyWindow in
            # the morning.  It makes for simple code:
            self.unmanage()


    def process_client_message_event(self, event):
        # FIXME
        # Need to listen for:
        #   _NET_CURRENT_DESKTOP
        #   _NET_WM_PING responses
        # and maybe:
        #   _NET_RESTACK_WINDOW
        #   _NET_WM_STATE (more fully)
        if event.message_type=="_NET_CLOSE_WINDOW":
            log.info("_NET_CLOSE_WINDOW received by %s", self)
            self.request_close()
            return True
        if event.message_type=="_NET_REQUEST_FRAME_EXTENTS":
            framelog("_NET_REQUEST_FRAME_EXTENTS")
            self._handle_frame_changed()
            return True
        if event.message_type=="_NET_MOVERESIZE_WINDOW":
            #this is overriden in WindowModel, skipped everywhere else:
            geomlog("_NET_MOVERESIZE_WINDOW skipped on %s (data=%s)", self, event.data)
            return True
        if event.message_type=="":
            log("empty message type: %s", event)
            if first_time("empty-x11-window-message-type-%#x" % event.window.get_xid()):
                log.warn("Warning: empty message type received for window %#x:", event.window.get_xid())
                log.warn(" %s", event)
                log.warn(" further messages will be silently ignored")
            return True
        #not handled:
        return False

    def do_xpra_configure_event(self, event):
        if self.client_window is None or not self._managed:
            return
        #shouldn't the border width always be 0?
        geom = (event.x, event.y, event.width, event.height)
        geomlog("CoreX11WindowModel.do_xpra_configure_event(%s) client_window=%#x, new geometry=%s",
                event, self.xid, geom)
        self._updateprop("geometry", geom)


    def do_xpra_shape_event(self, event):
        shapelog("shape event: %s, kind=%s", event, SHAPE_KIND.get(event.kind, event.kind))  # @UndefinedVariable
        cur_shape = self.get_property("shape")
        if cur_shape and cur_shape.get("serial", 0)>=event.serial:
            shapelog("same or older xshape serial no: %#x (current=%#x)", event.serial, cur_shape.get("serial", 0))
            return
        #remove serial before comparing dicts:
        cur_shape.pop("serial", None)
        #read new xshape:
        with xswallow:
            #should we pass the x and y offsets here?
            #v = self._read_xshape(event.x, event.y)
            if event.shaped:
                v = self._read_xshape()
            else:
                v = {}
            if cur_shape==v:
                shapelog("xshape unchanged")
                return
            v["serial"] = int(event.serial)
            shapelog("xshape updated with serial %#x", event.serial)
            self._internal_set_property("shape", v)


    def do_xpra_xkb_event(self, event):
        #X11: XKBNotify
        log("WindowModel.do_xpra_xkb_event(%r)" % event)
        if event.subtype!="bell":
            log.error("WindowModel.do_xpra_xkb_event(%r) unknown event type: %s" % (event, event.type))
            return
        event.window_model = self
        self.emit("bell", event)

    def do_xpra_client_message_event(self, event):
        #X11: ClientMessage
        log("do_xpra_client_message_event(%s)", event)
        if not event.data or len(event.data)!=5:
            log.warn("invalid event data: %s", event.data)
            return
        if not self.process_client_message_event(event):
            log.warn("do_xpra_client_message_event(%s) not handled", event)


    def do_xpra_focus_in_event(self, event):
        #X11: FocusIn
        grablog("focus_in_event(%s) mode=%s, detail=%s",
            event, GRAB_CONSTANTS.get(event.mode), DETAIL_CONSTANTS.get(event.detail, event.detail))
        if event.mode==NotifyNormal and event.detail==NotifyNonlinearVirtual:
            self.emit("restack", Above, None)
        else:
            self.may_emit_grab(event)

    def do_xpra_focus_out_event(self, event):
        #X11: FocusOut
        grablog("focus_out_event(%s) mode=%s, detail=%s",
            event, GRAB_CONSTANTS.get(event.mode), DETAIL_CONSTANTS.get(event.detail, event.detail))
        self.may_emit_grab(event)

    def may_emit_grab(self, event):
        if event.mode==NotifyGrab:
            grablog("emitting grab on %s", self)
            self.emit("grab", event)
        if event.mode==NotifyUngrab:
            grablog("emitting ungrab on %s", self)
            self.emit("ungrab", event)


    def do_xpra_motion_event(self, event):
        self.emit("motion", event)


    ################################
    # Actions
    ################################

    def raise_window(self):
        X11Window.XRaiseWindow(self.client_window.get_xid())

    def set_active(self):
        root = self.client_window.get_screen().get_root_window()
        prop_set(root, "_NET_ACTIVE_WINDOW", "u32", self.xid)


    ################################
    # Killing clients:
    ################################

    def request_close(self):
        if "WM_DELETE_WINDOW" in self.get_property("protocols"):
            self.send_delete()
        else:
            title = self.get_property("title")
            xid = self.get_property("xid")
            if FORCE_QUIT:
                log.info("window %#x ('%s') does not support WM_DELETE_WINDOW", xid, title)
                log.info(" using force quit")
                # You don't wanna play ball?  Then no more Mr. Nice Guy!
                self.force_quit()
            else:
                log.warn("window %#x ('%s') cannot be closed,", xid, title)
                log.warn(" it does not support WM_DELETE_WINDOW")
                log.warn(" and FORCE_QUIT is disabled")

    def send_delete(self):
        with xswallow:
            send_wm_delete_window(self.client_window)

    def XKill(self):
        with xswallow:
            X11Window.XKillClient(self.xid)

    def force_quit(self):
        machine = self.get_property("client-machine")
        pid = self.get_property("pid")
        if pid<=0:
            #we could fallback to _NET_WM_PID
            #but that would be unsafe
            log.warn("Warning: cannot terminate window %#x, no pid found", self.xid)
            if machine:
                log.warn(" WM_CLIENT_MACHINE=%s", machine)
            pid = self.get_property("wm-pid")
            if pid>0:
                log.warn(" _NET_WM_PID=%s", pid)
            return
        if pid==os.getpid():
            log.warn("Warning: force_quit is refusing to kill ourselves!")
            return
        localhost = gethostname()
        log("force_quit() pid=%s, machine=%s, localhost=%s", pid, machine, localhost)
        if machine is not None and machine == localhost:
            if self._kill_count==0:
                #first time around: just send a SIGINT and hope for the best
                try:
                    os.kill(pid, signal.SIGINT)
                except OSError as e:
                    log.warn("Warning: failed to kill(SIGINT) client with pid %s", pid)
                    log.warn(" %s", e)
            else:
                #the more brutal way: SIGKILL + XKill
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError as e:
                    log.warn("Warning: failed to kill(SIGKILL) client with pid %s", pid)
                    log.warn(" %s", e)
                self.XKill()
            self._kill_count += 1
            return
        self.XKill()
