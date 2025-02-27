# This file is part of Xpra.
# Copyright (C) 2010-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from gi.repository import GLib  # @UnresolvedImport

from xpra.platform.paths import get_icon_filename
from xpra.scripts.parsing import sound_option
from xpra.net.compression import Compressed
from xpra.net.protocol import CONNECTION_LOST
from xpra.os_util import get_machine_id, get_user_uuid, bytestostr, OSX, POSIX
from xpra.util import envint, typedict, csv, updict
from xpra.client.mixins.stub_client_mixin import StubClientMixin
from xpra.log import Logger

avsynclog = Logger("av-sync")
log = Logger("client", "sound")

AV_SYNC_DELTA = envint("XPRA_AV_SYNC_DELTA")
DELTA_THRESHOLD = envint("XPRA_AV_SYNC_DELTA_THRESHOLD", 40)
DEFAULT_AV_SYNC_DELAY = envint("XPRA_DEFAULT_AV_SYNC_DELAY", 150)


class AudioClient(StubClientMixin):
    """
    Utility mixin for clients that handle audio
    """
    __signals__ = ["speaker-changed", "microphone-changed"]

    def __init__(self):
        super().__init__()
        self.sound_source_plugin = None
        self.speaker_allowed = False
        self.speaker_enabled = False
        self.speaker_codecs = []
        self.microphone_allowed = False
        self.microphone_enabled = False
        self.microphone_codecs = []
        self.microphone_device = None
        self.av_sync = False
        self.av_sync_delta = AV_SYNC_DELTA
        #sound state:
        self.on_sink_ready = None
        self.sound_sink = None
        self.sound_sink_sequence = 0
        self.server_sound_eos_sequence = False
        self.sound_source = None
        self.sound_source_sequence = 0
        self.sound_in_bytecount = 0
        self.sound_out_bytecount = 0
        self.server_av_sync = False
        self.server_pulseaudio_id = None
        self.server_pulseaudio_server = None
        self.server_sound_decoders = []
        self.server_sound_encoders = []
        self.server_sound_receive = False
        self.server_sound_send = False
        self.queue_used_sent = None
        #duplicated from ServerInfo mixin:
        self._remote_machine_id = None

    def init(self, opts):
        self.av_sync = opts.av_sync
        self.sound_properties = typedict()
        self.speaker_allowed = sound_option(opts.speaker) in ("on", "off")
        #ie: "on", "off", "on:Some Device", "off:Some Device"
        mic = [x.strip() for x in opts.microphone.split(":", 1)]
        self.microphone_allowed = sound_option(mic[0]) in ("on", "off")
        self.microphone_device = None
        if self.microphone_allowed and len(mic)==2:
            self.microphone_device = mic[1]
        self.sound_source_plugin = opts.sound_source
        def sound_option_or_all(*_args):
            return []
        if self.speaker_allowed or self.microphone_allowed:
            try:
                from xpra.sound import common
                assert common
            except ImportError as e:
                self.may_notify_audio("No Audio",
                                      "audio subsystem is not installed\n" +
                                      " speaker and microphone forwarding are disabled")
                self.speaker_allowed = False
                self.microphone_allowed = False
            else:
                try:
                    from xpra.sound.common import sound_option_or_all
                    from xpra.sound.wrapper import query_sound
                    self.sound_properties = query_sound()
                    assert self.sound_properties, "query did not return any data"
                    gstv = self.sound_properties.strtupleget("gst.version")
                    if gstv:
                        log.info("GStreamer version %s", ".".join(gstv[:3]))
                    else:
                        log.info("GStreamer loaded")
                except Exception as e:
                    log("failed to query sound", exc_info=True)
                    log.error("Error: failed to query sound subsystem:")
                    log.error(" %s", e)
                    self.speaker_allowed = False
                    self.microphone_allowed = False
        encoders = self.sound_properties.strtupleget("encoders")
        decoders = self.sound_properties.strtupleget("decoders")
        self.speaker_codecs = sound_option_or_all("speaker-codec", opts.speaker_codec, decoders)
        self.microphone_codecs = sound_option_or_all("microphone-codec", opts.microphone_codec, encoders)
        if not self.speaker_codecs:
            self.speaker_allowed = False
        if not self.microphone_codecs:
            self.microphone_allowed = False
        self.speaker_enabled = self.speaker_allowed and sound_option(opts.speaker)=="on"
        self.microphone_enabled = self.microphone_allowed and opts.microphone.lower()=="on"
        log("speaker: codecs=%s, allowed=%s, enabled=%s", encoders, self.speaker_allowed, csv(self.speaker_codecs))
        log("microphone: codecs=%s, allowed=%s, enabled=%s, default device=%s",
            decoders, self.microphone_allowed, csv(self.microphone_codecs), self.microphone_device)
        log("av-sync=%s", self.av_sync)
        if POSIX and not OSX:
            try:
                from xpra.sound.pulseaudio.pulseaudio_util import get_info as get_pa_info
                pa_info = get_pa_info()
                log("pulseaudio info=%s", pa_info)
                self.sound_properties.update(pa_info)
            except ImportError as e:
                log.warn("Warning: no pulseaudio information available")
                log.warn(" %s", e)
            except Exception:
                log.error("failed to add pulseaudio info", exc_info=True)
        #audio tagging:
        self.init_audio_tagging(opts.tray_icon)


    def cleanup(self):
        self.stop_all_sound()


    def stop_all_sound(self):
        if self.sound_source:
            self.stop_sending_sound()
        if self.sound_sink:
            self.stop_receiving_sound()


    def get_info(self) -> dict:
        info = {
            "speaker" : self.speaker_enabled,
            "microphone" : self.microphone_enabled,
            "properties" : dict(self.sound_properties),
            }
        ss = self.sound_source
        if ss:
            info["src"] = ss.get_info()
        ss = self.sound_sink
        if ss:
            info["sink"] = ss.get_info()
        return {"audio" : info}


    def get_caps(self) -> dict:
        d = {}
        avcaps = self.get_avsync_capabilities()
        acaps = self.get_audio_capabilities()
        #legacy flat format:
        updict(d, "av-sync", avcaps)
        updict(d, "sound", acaps)
        #v4.4 namespace:
        d["av-sync"] = avcaps
        d["audio"] = acaps
        return d

    def get_audio_capabilities(self) -> dict:
        if not self.sound_properties:
            return {}
        #we don't know if the server supports new codec names,
        #so always add legacy names in hello:
        caps = {
            "codec-full-names"  : True,
            "decoders"   : self.speaker_codecs,
            "encoders"   : self.microphone_codecs,
            "send"       : self.microphone_allowed,
            "receive"    : self.speaker_allowed,
            }
        caps.update(self.sound_properties)
        log("audio capabilities: %s", caps)
        return caps

    def get_avsync_capabilities(self) -> dict:
        if not self.av_sync:
            return {}
        delay = max(0, DEFAULT_AV_SYNC_DELAY + AV_SYNC_DELTA)
        return {
            ""              : True,
            "enabled"       : True,
            "delay.default" : delay,
            "delay"         : delay,
            }


    def parse_server_capabilities(self, c : typedict) -> bool:
        self.server_av_sync = c.boolget("av-sync.enabled")
        avsynclog("av-sync: server=%s, client=%s", self.server_av_sync, self.av_sync)
        self.server_pulseaudio_id = c.strget("sound.pulseaudio.id")
        self.server_pulseaudio_server = c.strget("sound.pulseaudio.server")
        self.server_sound_decoders = c.strtupleget("sound.decoders")
        self.server_sound_encoders = c.strtupleget("sound.encoders")
        self.server_sound_receive = c.boolget("sound.receive")
        self.server_sound_send = c.boolget("sound.send")
        log("pulseaudio id=%s, server=%s, sound decoders=%s, sound encoders=%s, receive=%s, send=%s",
                 self.server_pulseaudio_id, self.server_pulseaudio_server,
                 csv(self.server_sound_decoders), csv(self.server_sound_encoders),
                 self.server_sound_receive, self.server_sound_send)
        if self.server_sound_send and self.speaker_enabled:
            self.show_progress(90, "starting speaker forwarding")
            self.start_receiving_sound()
        if self.server_sound_receive and self.microphone_enabled:
            #call via idle_add because we may query X11 properties
            #to find the pulseaudio server:
            GLib.idle_add(self.start_sending_sound)
        return True


    ######################################################################
    # audio:
    def init_audio_tagging(self, tray_icon):
        if not POSIX:
            return
        try:
            from xpra import sound
            assert sound
        except ImportError:
            log("no sound module, skipping pulseaudio tagging setup")
            return
        try:
            from xpra.sound.pulseaudio.pulseaudio_util import set_icon_path
            tray_icon_filename = get_icon_filename(tray_icon or "xpra")
            set_icon_path(tray_icon_filename)
        except ImportError as e:
            if not OSX:
                log.warn("Warning: failed to set pulseaudio tagging icon:")
                log.warn(" %s", e)


    def get_matching_codecs(self, local_codecs, server_codecs):
        matching_codecs = tuple(x for x in local_codecs if x in server_codecs)
        log("get_matching_codecs(%s, %s)=%s", local_codecs, server_codecs, matching_codecs)
        return matching_codecs

    def may_notify_audio(self, summary, body):
        #overriden in UI client subclass
        pass

    def audio_loop_check(self, mode="speaker"):
        from xpra.sound.gstreamer_util import ALLOW_SOUND_LOOP, loop_warning_messages
        if ALLOW_SOUND_LOOP:
            return True
        if self._remote_machine_id:
            if self._remote_machine_id!=get_machine_id():
                #not the same machine, so OK
                return True
            if self._remote_uuid!=get_user_uuid():
                #different user, assume different pulseaudio server
                return True
        #check pulseaudio id if we have it
        pulseaudio_id = self.sound_properties.get("pulseaudio", {}).get("id")
        if not pulseaudio_id or not self.server_pulseaudio_id:
            #not available, assume no pulseaudio so no loop?
            return True
        if self.server_pulseaudio_id!=pulseaudio_id:
            #different pulseaudio server
            return True
        msgs = loop_warning_messages(mode)
        summary = msgs[0]
        body = "\n".join(msgs[1:])
        self.may_notify_audio(summary, body)
        log.warn("Warning: %s", summary)
        for x in msgs[1:]:
            log.warn(" %s", x)
        return False

    def no_matching_codec_error(self, forwarding="speaker", server_codecs=(), client_codecs=()):
        summary = "Failed to start %s forwarding" % forwarding
        body = "No matching codecs between client and server"
        self.may_notify_audio(summary, body)
        log.error("Error: %s", summary)
        log.error(" server supports: %s", csv(server_codecs))
        log.error(" client supports: %s", csv(client_codecs))

    def start_sending_sound(self, device=None):
        """ (re)start a sound source and emit client signal """
        log("start_sending_sound(%s)", device)
        enabled = False
        try:
            assert self.microphone_allowed, "microphone forwarding is disabled"
            assert self.server_sound_receive, "client support for receiving sound is disabled"
            if not self.audio_loop_check("microphone"):
                return
            ss = self.sound_source
            if ss:
                if ss.get_state()=="active":
                    log.error("Error: microphone forwarding is already active")
                    enabled = True
                    return
                ss.start()
            else:
                enabled = self.start_sound_source(device)
        finally:
            if enabled!=self.microphone_enabled:
                self.microphone_enabled = enabled
                self.emit("microphone-changed")
            log("start_sending_sound(%s) done, microphone_enabled=%s", device, enabled)

    def start_sound_source(self, device=None):
        log("start_sound_source(%s)", device)
        assert self.sound_source is None
        def sound_source_state_changed(*_args):
            self.emit("microphone-changed")
        #find the matching codecs:
        matching_codecs = self.get_matching_codecs(self.microphone_codecs, self.server_sound_decoders)
        log("start_sound_source(%s) matching codecs: %s", device, csv(matching_codecs))
        if not matching_codecs:
            self.no_matching_codec_error("microphone", self.server_sound_decoders, self.microphone_codecs)
            return False
        try:
            from xpra.sound.wrapper import start_sending_sound
            plugins = self.sound_properties.get("plugins")
            ss = start_sending_sound(plugins, self.sound_source_plugin, device or self.microphone_device,
                                     None, 1.0, False, matching_codecs,
                                     self.server_pulseaudio_server, self.server_pulseaudio_id)
            if not ss:
                return False
            self.sound_source = ss
            ss.sequence = self.sound_source_sequence
            ss.connect("new-buffer", self.new_sound_buffer)
            ss.connect("state-changed", sound_source_state_changed)
            ss.connect("new-stream", self.new_stream)
            ss.start()
            log("start_sound_source(%s) sound source %s started", device, ss)
            return True
        except Exception as e:
            self.may_notify_audio("Failed to start microphone forwarding", "%s" % e)
            log.error("Error setting up microphone forwarding:")
            log.error(" %s", e)
            return False

    def new_stream(self, sound_source, codec):
        log("new_stream(%s)", codec)
        if self.sound_source!=sound_source:
            log("dropping new-stream signal (current source=%s, signal source=%s)", self.sound_source, sound_source)
            return
        codec = codec or sound_source.codec
        sound_source.codec = codec
        #tell the server this is the start:
        self.send("sound-data", codec, "",
                  {
                   "start-of-stream"    : True,
                   "codec"              : codec,
                   })

    def stop_sending_sound(self):
        """ stop the sound source and emit client signal """
        log("stop_sending_sound() sound source=%s", self.sound_source)
        ss = self.sound_source
        if self.microphone_enabled:
            self.microphone_enabled = False
            self.emit("microphone-changed")
        self.sound_source = None
        if ss is None:
            log.warn("Warning: cannot stop audio capture which has not been started")
            return
        #tell the server to stop:
        self.send("sound-data", ss.codec or "", "", {
            "end-of-stream" : True,
            "sequence"      : ss.sequence,
            })
        self.sound_source_sequence += 1
        ss.cleanup()

    def start_receiving_sound(self):
        """ ask the server to start sending sound and emit the client signal """
        log("start_receiving_sound() sound sink=%s", self.sound_sink)
        enabled = False
        try:
            if self.sound_sink is not None:
                log("start_receiving_sound: we already have a sound sink")
                enabled = True
                return
            if not self.server_sound_send:
                log.error("Error receiving sound: support not enabled on the server")
                return
            if not self.audio_loop_check("speaker"):
                return
            #choose a codec:
            matching_codecs = self.get_matching_codecs(self.speaker_codecs, self.server_sound_encoders)
            log("start_receiving_sound() matching codecs: %s", csv(matching_codecs))
            if not matching_codecs:
                self.no_matching_codec_error("speaker", self.server_sound_encoders, self.speaker_codecs)
                return
            codec = matching_codecs[0]
            def sink_ready(*args):
                scodec = codec
                log("sink_ready(%s) codec=%s (server codec name=%s)", args, codec, scodec)
                self.send("sound-control", "start", scodec)
                return False
            self.on_sink_ready = sink_ready
            enabled = self.start_sound_sink(codec)
        finally:
            if self.speaker_enabled!=enabled:
                self.speaker_enabled = enabled
                self.emit("speaker-changed")
            log("start_receiving_sound() done, speaker_enabled=%s", enabled)

    def stop_receiving_sound(self, tell_server=True):
        """ ask the server to stop sending sound, toggle flag so we ignore further packets and emit client signal """
        log("stop_receiving_sound(%s) sound sink=%s", tell_server, self.sound_sink)
        ss = self.sound_sink
        if self.speaker_enabled:
            self.speaker_enabled = False
            self.emit("speaker-changed")
        if not ss:
            return
        if tell_server and ss.sequence==self.sound_sink_sequence:
            self.send("sound-control", "stop", self.sound_sink_sequence)
        self.sound_sink_sequence += 1
        self.send("sound-control", "new-sequence", self.sound_sink_sequence)
        self.sound_sink = None
        log("stop_receiving_sound(%s) calling %s", tell_server, ss.cleanup)
        ss.cleanup()
        log("stop_receiving_sound(%s) done", tell_server)

    def sound_sink_state_changed(self, sound_sink, state):
        if sound_sink!=self.sound_sink:
            log("sound_sink_state_changed(%s, %s) not the current sink, ignoring it", sound_sink, state)
            return
        log("sound_sink_state_changed(%s, %s) on_sink_ready=%s", sound_sink, state, self.on_sink_ready)
        if bytestostr(state)=="ready" and self.on_sink_ready:
            if not self.on_sink_ready():
                self.on_sink_ready = None
        self.emit("speaker-changed")
    def sound_sink_bitrate_changed(self, sound_sink, bitrate):
        if sound_sink!=self.sound_sink:
            log("sound_sink_bitrate_changed(%s, %s) not the current sink, ignoring it", sound_sink, bitrate)
            return
        log("sound_sink_bitrate_changed(%s, %s)", sound_sink, bitrate)
        #not shown in the UI, so don't bother with emitting a signal:
        #self.emit("speaker-changed")
    def sound_sink_error(self, sound_sink, error):
        log("sound_sink_error(%s, %s) exit_code=%s, current sink=%s", sound_sink, error, self.exit_code, self.sound_sink)
        if self.exit_code is not None:
            #exiting
            return
        if sound_sink!=self.sound_sink:
            log("sound_sink_error(%s, %s) not the current sink, ignoring it", sound_sink, error)
            return
        estr = bytestostr(error).replace("gst-resource-error-quark: ", "")
        self.may_notify_audio("Speaker forwarding error", estr)
        log.warn("Error: stopping speaker:")
        log.warn(" %s", estr)
        self.stop_receiving_sound()
    def sound_process_stopped(self, sound_sink, *args):
        if self.exit_code is not None:
            #exiting
            return
        if sound_sink!=self.sound_sink:
            log("sound_process_stopped(%s, %s) not the current sink, ignoring it", sound_sink, args)
            return
        log.warn("Warning: the sound process has stopped")
        self.stop_receiving_sound()

    def sound_sink_exit(self, sound_sink, *args):
        log("sound_sink_exit(%s, %s) sound_sink=%s", sound_sink, args, self.sound_sink)
        if self.exit_code is not None:
            #exiting
            return
        ss = self.sound_sink
        if sound_sink!=ss:
            log("sound_sink_exit() not the current sink, ignoring it")
            return
        if ss and ss.codec:
            #the mandatory "I've been naughty warning":
            #we use the "codec" field as guard to ensure we only print this warning once..
            log.warn("Warning: the %s sound sink has stopped", ss.codec)
            ss.codec = ""
        self.stop_receiving_sound()

    def start_sound_sink(self, codec):
        log("start_sound_sink(%s)", codec)
        assert self.sound_sink is None, "sound sink already exists!"
        try:
            log("starting %s sound sink", codec)
            from xpra.sound.wrapper import start_receiving_sound
            ss = start_receiving_sound(codec)
            if not ss:
                return False
            ss.sequence = self.sound_sink_sequence
            self.sound_sink = ss
            ss.connect("state-changed", self.sound_sink_state_changed)
            ss.connect("error", self.sound_sink_error)
            ss.connect("exit", self.sound_sink_exit)
            ss.connect(CONNECTION_LOST, self.sound_process_stopped)
            ss.start()
            log("%s sound sink started", codec)
            return True
        except Exception as e:
            log.error("Error: failed to start sound sink", exc_info=True)
            self.sound_sink_error(self.sound_sink, e)
            return False

    def new_sound_buffer(self, sound_source, data, metadata, packet_metadata=()):
        log("new_sound_buffer(%s, %s, %s, %s)", sound_source, len(data or ()), metadata, packet_metadata)
        if sound_source.sequence<self.sound_source_sequence:
            log("sound buffer dropped: old sequence number: %s (current is %s)",
                sound_source.sequence, self.sound_source_sequence)
            return
        self.sound_out_bytecount += len(data)
        for x in packet_metadata:
            self.sound_out_bytecount += len(x)
        metadata["sequence"] = sound_source.sequence
        if packet_metadata:
            #the packet metadata is already compressed:
            packet_metadata = Compressed("packet metadata", packet_metadata, can_inline=True)
        self.send_sound_data(sound_source, data, metadata, packet_metadata)

    def send_sound_data(self, sound_source, data, metadata, packet_metadata=None):
        codec = sound_source.codec
        packet_data = [codec, Compressed(codec, data), metadata, packet_metadata or ()]
        self.send("sound-data", *packet_data)

    def send_sound_sync(self, v):
        self.send("sound-control", "sync", v)


    ######################################################################
    #packet handlers
    def _process_sound_data(self, packet):
        codec, data, metadata = packet[1:4]
        codec = bytestostr(codec)
        metadata = typedict(metadata)
        if data:
            self.sound_in_bytecount += len(data)
        #verify sequence number if present:
        seq = metadata.intget("sequence", -1)
        if self.sound_sink_sequence>0 and 0<=seq<self.sound_sink_sequence:
            log("ignoring sound data with old sequence number %s (now on %s)", seq, self.sound_sink_sequence)
            return

        if not self.speaker_enabled:
            if metadata.boolget("start-of-stream"):
                #server is asking us to start playing sound
                if not self.speaker_allowed:
                    #no can do!
                    log.warn("Warning: cannot honour the request to start the speaker")
                    log.warn(" speaker forwarding is disabled")
                    self.stop_receiving_sound(True)
                    return
                self.speaker_enabled = True
                self.emit("speaker-changed")
                self.on_sink_ready = None
                codec = metadata.strget("codec")
                log("starting speaker on server request using codec %s", codec)
                self.start_sound_sink(codec)
            else:
                log("speaker is now disabled - dropping packet")
                return
        ss = self.sound_sink
        if ss is None:
            log("no sound sink to process sound data, dropping it")
            return
        if metadata.boolget("end-of-stream"):
            log("server sent end-of-stream for sequence %s, closing sound pipeline", seq)
            self.stop_receiving_sound(False)
            return
        if codec!=ss.codec:
            log.error("Error: sound codec change is not supported!")
            log.error(" stream tried to switch from %s to %s", ss.codec, codec)
            self.stop_receiving_sound()
            return
        if ss.get_state()=="stopped":
            log("sound data received, sound sink is stopped - telling server to stop")
            self.stop_receiving_sound()
            return
        #the server may send packet_metadata, which is pushed before the actual sound data:
        packet_metadata = ()
        if len(packet)>4:
            packet_metadata = packet[4]
        #(some packets (ie: sos, eos) only contain metadata)
        if data or packet_metadata:
            ss.add_data(data, metadata, packet_metadata)
        if self.av_sync and self.server_av_sync:
            qinfo = typedict(ss.get_info()).dictget("queue")
            queue_used = typedict(qinfo or {}).intget("cur", None)
            if queue_used is None:
                return
            delta = (self.queue_used_sent or 0)-queue_used
            #avsynclog("server sound sync: queue info=%s, last sent=%s, delta=%s",
            #    dict((k,v) for (k,v) in info.items() if k.startswith("queue")), self.queue_used_sent, delta)
            if self.queue_used_sent is None or abs(delta)>=DELTA_THRESHOLD:
                avsynclog("server sound sync: sending updated queue.used=%i (was %s)",
                          queue_used, (self.queue_used_sent or "unset"))
                self.queue_used_sent = queue_used
                v = queue_used + self.av_sync_delta
                if self.av_sync_delta:
                    avsynclog(" adjusted value=%i with sync delta=%i", v, self.av_sync_delta)
                self.send_sound_sync(v)


    def init_authenticated_packet_handlers(self):
        log("init_authenticated_packet_handlers()")
        #these handlers can run directly from the network thread:
        self.add_packet_handler("sound-data", self._process_sound_data, False)
