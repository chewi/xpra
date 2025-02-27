# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2010-2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os

from xpra.net.compression import Compressed
from xpra.server.source.stub_source_mixin import StubSourceMixin
from xpra.os_util import get_machine_id, get_user_uuid, bytestostr, POSIX
from xpra.util import csv, envbool, envint, flatten_dict, typedict, XPRA_AUDIO_NOTIFICATION_ID
from xpra.log import Logger

log = Logger("sound")

NEW_STREAM_SOUND = envbool("XPRA_NEW_STREAM_SOUND", True)
NEW_STREAM_SOUND_STOP = envint("XPRA_NEW_STREAM_SOUND_STOP", 20)


class AudioMixin(StubSourceMixin):

    @classmethod
    def is_needed(cls, caps : typedict) -> bool:
        return caps.boolget("sound.send") or caps.boolget("sound.receive")


    def __init__(self):
        self.sound_properties = {}
        self.sound_source_plugin = ""
        self.supports_speaker = False
        self.speaker_codecs = []
        self.supports_microphone = False
        self.microphone_codecs = []

    def init_from(self, _protocol, server):
        self.sound_properties       = server.sound_properties
        self.sound_source_plugin    = server.sound_source_plugin
        self.supports_speaker       = server.supports_speaker
        self.supports_microphone    = server.supports_microphone
        self.speaker_codecs         = server.speaker_codecs
        self.microphone_codecs      = server.microphone_codecs

    def init_state(self):
        self.wants_sound = True
        self.sound_source_sequence = 0
        self.sound_source = None
        self.sound_sink = None
        self.pulseaudio_id = None
        self.pulseaudio_cookie_hash = None
        self.pulseaudio_server = None
        self.sound_decoders = ()
        self.sound_encoders = ()
        self.sound_receive = False
        self.sound_send = False
        self.sound_fade_timer = None
        self.new_stream_timers = {}

    def cleanup(self):
        log("%s.cleanup()", self)
        self.cancel_sound_fade_timer()
        self.stop_sending_sound()
        self.stop_receiving_sound()
        self.stop_new_stream_notifications()
        self.init_state()


    def stop_new_stream_notifications(self):
        timers = self.new_stream_timers.copy()
        self.new_stream_timers = {}
        for proc, timer in timers.items():
            timer = self.new_stream_timers.pop(proc, None)
            if timer:
                self.source_remove(timer)
            self.stop_new_stream_notification(proc)

    def stop_new_stream_notification(self, proc):
        r = proc.poll()
        log("stop_new_stream_notification(%s) exit code=%s", proc, r)
        if r is not None:
            #already ended
            return
        try:
            proc.terminate()
        except Exception:
            log("failed to stop stream notification %s", proc)


    def parse_client_caps(self, c):
        self.wants_sound = c.boolget("wants_sound", True) or "sound" in c.strtupleget("wants")
        audio = c.dictget("audio")
        if audio:
            audio = typedict(audio)
            self.pulseaudio_id = audio.strget("pulseaudio.id")
            self.pulseaudio_cookie_hash = audio.strget("pulseaudio.cookie-hash")
            self.pulseaudio_server = audio.strget("pulseaudio.server")
            self.sound_decoders = audio.strtupleget("decoders", [])
            self.sound_encoders = audio.strtupleget("encoders", [])
            self.sound_receive = audio.boolget("receive")
            self.sound_send = audio.boolget("send")
        else:
            #pre v4.4:
            self.pulseaudio_id = c.strget("sound.pulseaudio.id")
            self.pulseaudio_cookie_hash = c.strget("sound.pulseaudio.cookie-hash")
            self.pulseaudio_server = c.strget("sound.pulseaudio.server")
            self.sound_decoders = c.strtupleget("sound.decoders", [])
            self.sound_encoders = c.strtupleget("sound.encoders", [])
            self.sound_receive = c.boolget("sound.receive")
            self.sound_send = c.boolget("sound.send")
        log("pulseaudio id=%s, cookie-hash=%s, server=%s, sound decoders=%s, sound encoders=%s, receive=%s, send=%s",
                 self.pulseaudio_id, self.pulseaudio_cookie_hash, self.pulseaudio_server,
                 self.sound_decoders, self.sound_encoders, self.sound_receive, self.sound_send)

    def get_caps(self) -> dict:
        if not self.wants_sound or not self.sound_properties:
            return {}
        sound_props = self.sound_properties.copy()
        sound_props.update({
            "codec-full-names"  : True,
            "encoders"          : self.speaker_codecs,
            "decoders"          : self.microphone_codecs,
            "send"              : self.supports_speaker and len(self.speaker_codecs)>0,
            "receive"           : self.supports_microphone and len(self.microphone_codecs)>0,
            })
        caps = flatten_dict({"sound" : sound_props})
        caps["audio"] = sound_props
        return caps


    def audio_loop_check(self, mode="speaker") -> bool:
        log("audio_loop_check(%s)", mode)
        from xpra.sound.gstreamer_util import ALLOW_SOUND_LOOP, loop_warning_messages
        if ALLOW_SOUND_LOOP:
            return True
        machine_id = get_machine_id()
        uuid = get_user_uuid()
        #these attributes belong in a different mixin,
        #so we can't assume that they exist:
        client_machine_id = getattr(self, "machine_id", None)
        client_uuid = getattr(self, "uuid", None)
        log("audio_loop_check(%s) machine_id=%s client machine_id=%s, uuid=%s, client uuid=%s",
            mode, machine_id, client_machine_id, uuid, client_uuid)
        if client_machine_id:
            if client_machine_id!=machine_id:
                #not the same machine, so OK
                return True
            if client_uuid!=uuid:
                #different user, assume different pulseaudio server
                return True
        #check pulseaudio id if we have it
        pulseaudio_id = self.sound_properties.get("pulseaudio", {}).get("id")
        pulseaudio_cookie_hash = self.sound_properties.get("pulseaudio", {}).get("cookie-hash")
        log("audio_loop_check(%s) pulseaudio id=%s, client pulseaudio id=%s",
                 mode, pulseaudio_id, self.pulseaudio_id)
        log("audio_loop_check(%s) pulseaudio cookie hash=%s, client pulseaudio cookie hash=%s",
                 mode, pulseaudio_cookie_hash, self.pulseaudio_cookie_hash)
        if pulseaudio_id and self.pulseaudio_id:
            if self.pulseaudio_id!=pulseaudio_id:
                return True
        elif pulseaudio_cookie_hash and self.pulseaudio_cookie_hash:
            if self.pulseaudio_cookie_hash!=pulseaudio_cookie_hash:
                return True
        else:
            #no cookie or id, so probably not a pulseaudio setup,
            #hope for the best:
            return True
        msgs = loop_warning_messages(mode)
        summary = msgs[0]
        body = "\n".join(msgs[1:])
        nid = XPRA_AUDIO_NOTIFICATION_ID
        self.may_notify(nid, summary, body, icon_name=mode)
        log.warn("Warning: %s", summary)
        for x in msgs[1:]:
            log.warn(" %s", x)
        return False

    def start_sending_sound(self, codec=None, volume=1.0,
                            new_stream=None, new_buffer=None, skip_client_codec_check=False):
        log("start_sending_sound(%s)", codec)
        ss = None
        if getattr(self, "suspended", False):
            log.warn("Warning: not starting sound whilst in suspended state")
            return None
        if not self.supports_speaker:
            log.error("Error sending sound: support not enabled on the server")
            return None
        if self.sound_source:
            log.error("Error sending sound: forwarding already in progress")
            return None
        if not self.sound_receive:
            log.error("Error sending sound: support is not enabled on the client")
            return None
        if codec is None:
            codecs = [x for x in self.sound_decoders if x in self.speaker_codecs]
            if not codecs:
                log.error("Error sending sound: no codecs in common")
                return None
            codec = codecs[0]
        elif codec not in self.speaker_codecs:
            log.warn("Warning: invalid codec specified: %s", codec)
            return None
        elif (codec not in self.sound_decoders) and not skip_client_codec_check:
            log.warn("Error sending sound: invalid codec '%s'", codec)
            log.warn(" is not in the list of decoders supported by the client: %s", csv(self.sound_decoders))
            return None
        if not self.audio_loop_check("speaker"):
            return None
        try:
            from xpra.sound.wrapper import start_sending_sound
            plugins = self.sound_properties.strtupleget("plugins")
            ss = start_sending_sound(plugins, self.sound_source_plugin,
                                     None, codec, volume, True, [codec],
                                     self.pulseaudio_server, self.pulseaudio_id)
            self.sound_source = ss
            log("start_sending_sound() sound source=%s", ss)
            if not ss:
                return None
            ss.sequence = self.sound_source_sequence
            ss.connect("new-buffer", new_buffer or self.new_sound_buffer)
            ss.connect("new-stream", new_stream or self.new_stream)
            ss.connect("info", self.sound_source_info)
            ss.connect("exit", self.sound_source_exit)
            ss.connect("error", self.sound_source_error)
            ss.start()
            return ss
        except Exception as e:
            log.error("error setting up sound: %s", e, exc_info=True)
            self.stop_sending_sound()
            ss = None
            return None
        finally:
            if ss is None:
                #tell the client we're not sending anything:
                self.send_eos(codec)

    def sound_source_error(self, source, message):
        #this should be printed to stderr by the sound process already
        if source==self.sound_source:
            log("audio capture error: %s", message)

    def sound_source_exit(self, source, *args):
        log("sound_source_exit(%s, %s)", source, args)
        if source==self.sound_source:
            self.stop_sending_sound()

    def sound_source_info(self, source, info):
        log("sound_source_info(%s, %s)", source, info)

    def stop_sending_sound(self):
        ss = self.sound_source
        log("stop_sending_sound() sound_source=%s", ss)
        if ss:
            self.sound_source = None
            self.send_eos(ss.codec, ss.sequence)
            self.sound_source_sequence += 1
            ss.cleanup()
        self.call_update_av_sync_delay()

    def send_eos(self, codec, sequence=0):
        log("send_eos(%s, %s)", codec, sequence)
        #tell the client this is the end:
        self.send_more("sound-data", codec, "",
                       {
                           "end-of-stream" : True,
                           "sequence"      : sequence,
                        })


    def new_stream(self, sound_source, codec):
        if NEW_STREAM_SOUND:
            try:
                from xpra.platform.paths import get_resources_dir
                sample = os.path.join(get_resources_dir(), "bell.wav")
                log("new_stream(%s, %s) sample=%s, exists=%s", sound_source, codec, sample, os.path.exists(sample))
                if os.path.exists(sample):
                    if POSIX:
                        sink = "alsasink"
                    else:
                        sink = "autoaudiosink"
                    cmd = [
                        "gst-launch-1.0", "-q",
                        "filesrc", "location=%s" % sample,
                        "!", "decodebin",
                        "!", "audioconvert",
                        "!", sink]
                    import subprocess
                    proc = subprocess.Popen(cmd)
                    log("Popen(%s)=%s", cmd, proc)
                    from xpra.child_reaper import getChildReaper
                    getChildReaper().add_process(proc, "new-stream-sound", cmd, ignore=True, forget=True)
                    def stop_new_stream_notification():
                        if self.new_stream_timers.pop(proc, None):
                            self.stop_new_stream_notification(proc)
                    timer = self.timeout_add(NEW_STREAM_SOUND_STOP*1000, stop_new_stream_notification)
                    self.new_stream_timers[proc] = timer
            except Exception as e:
                log("new_stream(%s, %s) error playing new stream sound", sound_source, codec, exc_info=True)
                log.error("Error playing new-stream bell sound:")
                log.error(" %s", e)
        log("new_stream(%s, %s)", sound_source, codec)
        if self.sound_source!=sound_source:
            log("dropping new-stream signal (current source=%s, signal source=%s)", self.sound_source, sound_source)
            return
        codec = codec or sound_source.codec
        sound_source.codec = codec
        #tell the client this is the start:
        self.send("sound-data", codec, "",
                  {
                   "start-of-stream"    : True,
                   "codec"              : codec,
                   "sequence"           : sound_source.sequence,
                   })
        self.call_update_av_sync_delay()
        #run it again after 10 seconds,
        #by that point the source info will actually be populated:
        from gi.repository import GLib
        GLib.timeout_add(10*1000, self.call_update_av_sync_delay)

    def call_update_av_sync_delay(self):
        #loose coupling with avsync mixin:
        update_av_sync = getattr(self, "update_av_sync_delay_total", None)
        log("call_update_av_sync_delay update_av_sync=%s", update_av_sync)
        if callable(update_av_sync):
            update_av_sync()  #pylint: disable=not-callable


    def new_sound_buffer(self, sound_source, data, metadata, packet_metadata=None):
        log("new_sound_buffer(%s, %s, %s, %s) info=%s",
                 sound_source, len(data or []), metadata, [len(x) for x in packet_metadata], sound_source.info)
        if self.sound_source!=sound_source or self.is_closed():
            log("sound buffer dropped: from old source or closed")
            return
        if sound_source.sequence<self.sound_source_sequence:
            log("sound buffer dropped: old sequence number: %s (current is %s)",
                sound_source.sequence, self.sound_source_sequence)
            return
        if packet_metadata:
            #the packet metadata is compressed already:
            packet_metadata = Compressed("packet metadata", packet_metadata, can_inline=True)
        #don't drop the first 10 buffers
        can_drop_packet = (sound_source.info or {}).get("buffer_count", 0)>10
        self.send_sound_data(sound_source, data, metadata, packet_metadata, can_drop_packet)

    def send_sound_data(self, sound_source, data, metadata, packet_metadata=None, can_drop_packet=False):
        packet_data = [sound_source.codec, Compressed(sound_source.codec, data), metadata, packet_metadata or ()]
        sequence = sound_source.sequence
        if sequence>=0:
            metadata["sequence"] = sequence
        fail_cb = None
        if can_drop_packet:
            def sound_data_fail_cb():
                #ideally we would tell gstreamer to send an audio "key frame"
                #or synchronization point to ensure the stream recovers
                log("a sound data buffer was not received and will not be resent")
            fail_cb = sound_data_fail_cb
        self.send("sound-data", *packet_data, synchronous=False, fail_cb=fail_cb, will_have_more=True)

    def stop_receiving_sound(self):
        ss = self.sound_sink
        log("stop_receiving_sound() sound_sink=%s", ss)
        if ss:
            self.sound_sink = None
            ss.cleanup()


    ##########################################################################
    # sound control commands:
    def sound_control(self, action, *args):
        action = bytestostr(action)
        log("sound_control(%s, %s)", action, args)
        method = getattr(self, "sound_control_%s" % (action.replace("-", "_")), None)
        if method is None:
            msg = "unknown sound action: %s" % action
            log.error(msg)
            return msg
        return method(*args)  #pylint: disable=not-callable

    def sound_control_stop(self, sequence_str=""):
        if sequence_str:
            try:
                sequence = int(sequence_str)
            except ValueError:
                msg = "sound sequence number '%s' is invalid" % sequence_str
                log.warn(msg)
                return msg
            if sequence!=self.sound_source_sequence:
                log.warn("Warning: sound sequence mismatch: %i vs %i",
                         sequence, self.sound_source_sequence)
                return "not stopped"
            log("stop: sequence number matches")
        self.stop_sending_sound()
        return "stopped"

    def sound_control_fadein(self, codec="", delay_str=""):
        self.do_sound_control_start(0.0, codec)
        delay = 1000
        if delay_str:
            delay = max(1, min(10*1000, int(delay_str)))
        step = 1.0/(delay/100.0)
        log("sound_control fadein delay=%s, step=%1.f", delay, step)
        def fadein():
            ss = self.sound_source
            if not ss:
                return False
            volume = ss.get_volume()
            log("fadein() volume=%.1f", volume)
            if volume<1.0:
                volume = min(1.0, volume+step)
                ss.set_volume(volume)
            return volume<1.0
        self.cancel_sound_fade_timer()
        self.sound_fade_timer = self.timeout_add(100, fadein)

    def sound_control_start(self, codec=""):
        self.do_sound_control_start(1.0, codec)

    def do_sound_control_start(self, volume, codec):
        codec = bytestostr(codec)
        log("do_sound_control_start(%s, %s)", volume, codec)
        if not self.start_sending_sound(codec, volume):
            return "failed to start sound"
        msg = "sound started"
        if codec:
            msg += " using codec %s" % codec
        return msg

    def sound_control_fadeout(self, delay_str=""):
        assert self.sound_source, "no active audio capture"
        delay = 1000
        if delay_str:
            delay = max(1, min(10*1000, int(delay_str)))
        step = 1.0/(delay/100.0)
        log("sound_control fadeout delay=%s, step=%1.f", delay, step)
        def fadeout():
            ss = self.sound_source
            if not ss:
                return False
            volume = ss.get_volume()
            log("fadeout() volume=%.1f", volume)
            if volume>0:
                ss.set_volume(max(0, volume-step))
                return True
            self.stop_sending_sound()
            return False
        self.cancel_sound_fade_timer()
        self.sound_fade_timer = self.timeout_add(100, fadeout)

    def sound_control_new_sequence(self, seq_str):
        self.sound_source_sequence = int(seq_str)
        return "new sequence is %s" % self.sound_source_sequence


    def cancel_sound_fade_timer(self):
        sft = self.sound_fade_timer
        if sft:
            self.sound_fade_timer = None
            self.source_remove(sft)

    def sound_data(self, codec, data, metadata, packet_metadata=()):
        log("sound_data(%s, %s, %s, %s) sound sink=%s",
            codec, len(data or []), metadata, packet_metadata, self.sound_sink)
        if self.is_closed():
            return
        if self.sound_sink is not None and codec!=self.sound_sink.codec:
            log.info("sound codec changed from %s to %s", self.sound_sink.codec, codec)
            self.sound_sink.cleanup()
            self.sound_sink = None
        if metadata.get("end-of-stream"):
            log("client sent end-of-stream, closing sound pipeline")
            self.stop_receiving_sound()
            return
        if not self.sound_sink:
            if not self.audio_loop_check("microphone"):
                #make a fake object so we don't fire the audio loop check warning repeatedly
                class FakeSink:
                    def __init__(self, codec):
                        self.codec = codec
                    def add_data(self, *args):
                        log("FakeSink.add_data%s ignored", args)
                    def cleanup(self, *args):
                        log("FakeSink.cleanup%s ignored", args)
                self.sound_sink = FakeSink(codec)
                return
            try:
                def sound_sink_error(*args):
                    log("sound_sink_error%s", args)
                    log.warn("Warning: stopping sound input because of an error")
                    self.stop_receiving_sound()
                from xpra.sound.wrapper import start_receiving_sound
                ss = start_receiving_sound(codec)
                if not ss:
                    return
                self.sound_sink = ss
                log("sound_data(..) created sound sink: %s", self.sound_sink)
                ss.connect("error", sound_sink_error)
                ss.start()
                log("sound_data(..) sound sink started")
            except Exception:
                log.error("Error: failed to start receiving %r", codec, exc_info=True)
                return
        self.sound_sink.add_data(data, metadata, packet_metadata)


    def get_sound_source_latency(self):
        encoder_latency = 0
        ss = self.sound_source
        cinfo = ""
        if ss:
            info = typedict(ss.info or {})
            try:
                qdict = info.dictget("queue")
                if qdict:
                    q = typedict(qdict).intget("cur", 0)
                    log("server side queue level: %s", q)
                #get the latency from the source info, if it has it:
                encoder_latency = info.intget("latency", -1)
                if encoder_latency<0:
                    #fallback to hard-coded values:
                    from xpra.sound.gstreamer_util import ENCODER_LATENCY, RECORD_PIPELINE_LATENCY
                    encoder_latency = RECORD_PIPELINE_LATENCY + ENCODER_LATENCY.get(ss.codec, 0)
                    cinfo = "%s " % ss.codec
                #processing overhead
                encoder_latency += 100
            except Exception as e:
                encoder_latency = 0
                log("failed to get encoder latency for %s: %s", ss.codec, e)
        log("get_sound_source_latency() %s: %s", cinfo, encoder_latency)
        return encoder_latency


    def get_info(self) -> dict:
        return {"sound" : self.get_sound_info()}

    def get_sound_info(self) -> dict:
        def sound_info(supported, prop, codecs):
            i = {"codecs" : codecs}
            if not supported:
                i["state"] = "disabled"
                return i
            if prop is None:
                i["state"] = "inactive"
                return i
            i.update(prop.get_info())
            return i
        info = {
                "speaker"       : sound_info(self.supports_speaker, self.sound_source, self.sound_decoders),
                "microphone"    : sound_info(self.supports_microphone, self.sound_sink, self.sound_encoders),
                }
        for prop in ("pulseaudio_id", "pulseaudio_server"):
            v = getattr(self, prop)
            if v is not None:
                info[prop] = v
        return info
