# This file is part of Xpra.
# Copyright (C) 2018-2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import os
import re
import shlex
import socket
from time import sleep, monotonic
from subprocess import PIPE, Popen

from xpra.scripts.main import (
    InitException, InitExit,
    shellquote, host_target_string,
    )
from xpra.platform.paths import get_ssh_known_hosts_files
from xpra.platform.info import get_username
from xpra.scripts.config import parse_bool, TRUE_OPTIONS
from xpra.scripts.pinentry_wrapper import input_pass, confirm
from xpra.net.bytestreams import SocketConnection, SOCKET_TIMEOUT, ConnectionClosedException
from xpra.make_thread import start_thread
from xpra.exit_codes import (
    EXIT_SSH_KEY_FAILURE, EXIT_SSH_FAILURE,
    EXIT_CONNECTION_FAILED,
    )
from xpra.os_util import (
    bytestostr, osexpand, load_binary_file,
    nomodule_context, umask_context,
    restore_script_env, get_saved_env,
    WIN32, OSX, POSIX,
    )
from xpra.util import envint, envbool, envfloat, engs, noerr, csv
from xpra.log import Logger, is_debug_enabled

#pylint: disable=import-outside-toplevel

log = Logger("network", "ssh")
if log.is_debug_enabled():
    import logging
    logging.getLogger("paramiko").setLevel(logging.DEBUG)


INITENV_COMMAND = os.environ.get("XPRA_INITENV_COMMAND", "")    #"xpra initenv"
WINDOW_SIZE = envint("XPRA_SSH_WINDOW_SIZE", 2**27-1)
TIMEOUT = envint("XPRA_SSH_TIMEOUT", 60)

VERIFY_HOSTKEY = envbool("XPRA_SSH_VERIFY_HOSTKEY", True)
VERIFY_STRICT = envbool("XPRA_SSH_VERIFY_STRICT", False)
ADD_KEY = envbool("XPRA_SSH_ADD_KEY", True)
#which authentication mechanisms are enabled with paramiko:
NONE_AUTH = envbool("XPRA_SSH_NONE_AUTH", True)
PASSWORD_AUTH = envbool("XPRA_SSH_PASSWORD_AUTH", True)
AGENT_AUTH = envbool("XPRA_SSH_AGENT_AUTH", True)
KEY_AUTH = envbool("XPRA_SSH_KEY_AUTH", True)
PASSWORD_RETRY = envint("XPRA_SSH_PASSWORD_RETRY", 2)
SSH_AGENT = envbool("XPRA_SSH_AGENT", False)
assert PASSWORD_RETRY>=0
LOG_FAILED_CREDENTIALS = envbool("XPRA_LOG_FAILED_CREDENTIALS", False)
MAGIC_QUOTES = envbool("XPRA_SSH_MAGIC_QUOTES", True)
TEST_COMMAND_TIMEOUT = envint("XPRA_SSH_TEST_COMMAND_TIMEOUT", 10)
EXEC_STDOUT_TIMEOUT = envfloat("XPRA_SSH_EXEC_STDOUT_TIMEOUT", 2)
EXEC_STDERR_TIMEOUT = envfloat("XPRA_SSH_EXEC_STDERR_TIMEOUT", 0)

MSYS_DEFAULT_PATH = os.environ.get("XPRA_MSYS_DEFAULT_PATH", "/mingw64/bin/xpra")
CYGWIN_DEFAULT_PATH = os.environ.get("XPRA_CYGWIN_DEFAULT_PATH", "/cygdrive/c/Program Files/Xpra/Xpra_cmd.exe")
DEFAULT_WIN32_INSTALL_PATH = "C:\\Program Files\\Xpra"

PARAMIKO_SESSION_LOST = "No existing session"


def keymd5(k) -> str:
    import binascii
    f = bytestostr(binascii.hexlify(k.get_fingerprint()))
    s = "MD5"
    while f:
        s += ":"+f[:2]
        f = f[2:]
    return s


class SSHSocketConnection(SocketConnection):

    def __init__(self, ssh_channel, sock, sockname, peername, target, info=None, socket_options=None):
        self._raw_socket = sock
        super().__init__(ssh_channel, sockname, peername, target, "ssh", info, socket_options)

    def get_raw_socket(self):
        return self._raw_socket

    def start_stderr_reader(self):
        start_thread(self._stderr_reader, "ssh-stderr-reader", daemon=True)

    def _stderr_reader(self):
        #stderr = self._socket.makefile_stderr(mode="rb", bufsize=1)
        chan = self._socket
        stderr = chan.makefile_stderr("rb", 1)
        while self.active:
            v = stderr.readline()
            if not v:
                log.info("SSH EOF on stderr of %s", chan.get_name())
                break
            s = bytestostr(v.rstrip(b"\n\r"))
            if s:
                log.info(" SSH: %r", s)

    def peek(self, n):
        if not self._raw_socket:
            return None
        return self._raw_socket.recv(n, socket.MSG_PEEK)

    def get_socket_info(self) -> dict:
        if not self._raw_socket:
            return {}
        return self.do_get_socket_info(self._raw_socket)

    def get_info(self) -> dict:
        i = super().get_info()
        s = self._socket
        if s:
            i["ssh-channel"] = {
                "id"    : s.get_id(),
                "name"  : s.get_name(),
                }
        return i


class SSHProxyCommandConnection(SSHSocketConnection):
    def __init__(self, ssh_channel, peername, target, info):
        super().__init__(ssh_channel, None, None, peername, target, info)
        self.process = None

    def error_is_closed(self, e) -> bool:
        p = self.process
        if p:
            #if the process has terminated,
            #then the connection must be closed:
            if p[0].poll() is not None:
                return True
        return super().error_is_closed(e)

    def get_socket_info(self) -> dict:
        p = self.process
        if not p:
            return {}
        proc, _ssh, cmd = p
        return {
            "process" : {
                "pid"       : proc.pid,
                "returncode": proc.returncode,
                "command"   : cmd,
                }
            }

    def close(self):
        try:
            super().close()
        except Exception:
            #this can happen if the proxy command gets a SIGINT,
            #it's closed already and we don't care
            log("SSHProxyCommandConnection.close()", exc_info=True)


def safe_lookup(config_obj, host):
    try:
        return config_obj.lookup(host)
    except ImportError as e:
        log("%s.lookup(%s)", config_obj, host, exc_info=True)
        log.warn(f"Warning: unable to load SSH host config for {host!r}:")
        log.warn(f" {e}")
    return {}

def ssh_paramiko_connect_to(display_desc):
    #plain socket attributes:
    host = display_desc["host"]
    port = display_desc.get("port", 22)
    #ssh and command attributes:
    username = display_desc.get("username") or get_username()
    if "proxy_host" in display_desc:
        display_desc.setdefault("proxy_username", get_username())
    password = display_desc.get("password")
    remote_xpra = display_desc["remote_xpra"]
    proxy_command = display_desc["proxy_command"]       #ie: "_proxy_start"
    socket_dir = display_desc.get("socket_dir")
    display = display_desc.get("display")
    display_as_args = display_desc["display_as_args"]   #ie: "--start=xterm :10"
    paramiko_config = display_desc.copy()
    paramiko_config.update(display_desc.get("paramiko-config", {}))
    socket_info = {
            "host"  : host,
            "port"  : port,
            }
    def get_keyfiles(host_config, config_name="key"):
        keyfiles = (host_config or {}).get("identityfile") or get_default_keyfiles()
        keyfile = paramiko_config.get(config_name)
        if keyfile:
            keyfiles.insert(0, keyfile)
        return keyfiles

    def fail(msg):
        log("ssh_paramiko_connect_to(%s)", display_desc, exc_info=True)
        raise InitExit(EXIT_SSH_FAILURE, msg) from None

    with nogssapi_context():
        from paramiko import SSHConfig, ProxyCommand
        ssh_config = SSHConfig()
        def ssh_lookup(key):
            return safe_lookup(ssh_config, key)
        user_config_file = os.path.expanduser("~/.ssh/config")
        sock = None
        host_config = None
        if os.path.exists(user_config_file):
            with open(user_config_file, "r", encoding="utf8") as f:
                try:
                    ssh_config.parse(f)
                except Exception as e:
                    log(f"parse({user_config_file})", exc_info=True)
                    log.error(f"Error parsing {user_config_file!r}:")
                    log.estr(e)
            log(f"parsed user config {user_config_file!r}")
            try:
                log("%i hosts found", len(ssh_config.get_hostnames()))
            except KeyError:
                pass
            host_config = ssh_lookup(host)
            if host_config:
                log(f"got host config for {host!r}: {host_config}")
                host = host_config.get("hostname", host)
                if "username" not in display_desc:
                    username = host_config.get("user", username)
                if "port" not in display_desc:
                    port = host_config.get("port", port)
                    try:
                        port = int(port)
                    except (TypeError, ValueError):
                        raise InitExit(EXIT_SSH_FAILURE, f"invalid ssh port specified: {port!r}") from None
                proxycommand = host_config.get("proxycommand")
                if proxycommand:
                    log(f"found proxycommand={proxycommand!r} for host {host!r}")
                    sock = ProxyCommand(proxycommand)
                    log(f"ProxyCommand({proxycommand})={sock}")
                    from xpra.child_reaper import getChildReaper
                    cmd = getattr(sock, "cmd", [])
                    def proxycommand_ended(proc):
                        log(f"proxycommand_ended({proc}) exit code={proc.poll()}")
                    getChildReaper().add_process(sock.process, "paramiko-ssh-client", cmd, True, True,
                                                 callback=proxycommand_ended)
                    proxy_keys = get_keyfiles(host_config, "proxy_key")
                    log(f"proxy keys={proxy_keys}")
                    from paramiko.client import SSHClient
                    ssh_client = SSHClient()
                    ssh_client.load_system_host_keys()
                    log("ssh proxy command connect to %s", (host, port, sock))
                    ssh_client.connect(host, port, sock=sock)
                    transport = ssh_client.get_transport()
                    do_ssh_paramiko_connect_to(transport, host,
                                               username, password,
                                               host_config or ssh_lookup("*"),
                                               proxy_keys,
                                               paramiko_config)
                    chan = paramiko_run_remote_xpra(transport, proxy_command, remote_xpra, socket_dir, display_as_args)
                    peername = (host, port)
                    conn = SSHProxyCommandConnection(chan, peername, peername, socket_info)
                    conn.target = host_target_string("ssh", username, host, port, display)
                    conn.timeout = SOCKET_TIMEOUT
                    conn.start_stderr_reader()
                    conn.process = (sock.process, "ssh", cmd)
                    from xpra.net import bytestreams
                    from paramiko.ssh_exception import ProxyCommandFailure
                    bytestreams.CLOSED_EXCEPTIONS = tuple(list(bytestreams.CLOSED_EXCEPTIONS)+[ProxyCommandFailure])
                    return conn

        keys = get_keyfiles(host_config)
        from xpra.net.socket_util import socket_connect
        if "proxy_host" in display_desc:
            proxy_host = display_desc["proxy_host"]
            proxy_port = int(display_desc.get("proxy_port", 22))
            proxy_username = display_desc.get("proxy_username", username)
            proxy_password = display_desc.get("proxy_password", password)
            proxy_keys = get_keyfiles(host_config, "proxy_key")
            sock = socket_connect(proxy_host, proxy_port)
            if not sock:
                fail(f"SSH proxy transport failed to connect to {proxy_host}:{proxy_port}")
            middle_transport = do_ssh_paramiko_connect(sock, proxy_host,
                                                       proxy_username, proxy_password,
                                                       ssh_lookup(host) or ssh_lookup("*"),
                                                       proxy_keys,
                                                       paramiko_config)
            log("Opening proxy channel")
            chan_to_middle = middle_transport.open_channel("direct-tcpip", (host, port), ("localhost", 0))
            transport = do_ssh_paramiko_connect(chan_to_middle, host,
                                                username, password,
                                                host_config or ssh_lookup("*"),
                                                keys,
                                                paramiko_config)
            chan = paramiko_run_remote_xpra(transport, proxy_command, remote_xpra, socket_dir, display_as_args)
            peername = (host, port)
            conn = SSHProxyCommandConnection(chan, peername, peername, socket_info)
            conn.target = host_target_string("ssh", username, host, port, display) \
                            + " via " + \
                            host_target_string("ssh", proxy_username, proxy_host, proxy_port, None)
            conn.timeout = SOCKET_TIMEOUT
            conn.start_stderr_reader()
            return conn

        #plain TCP connection to the server,
        #we open it then give the socket to paramiko:
        auth_modes = get_auth_modes(paramiko_config, host_config, password)
        log(f"authentication modes={auth_modes}")
        sock = None
        while True:
            sock = socket_connect(host, port)
            if not sock:
                fail(f"SSH failed to connect to {host}:{port}")
            sockname = sock.getsockname()
            peername = sock.getpeername()
            log(f"paramiko socket_connect: sockname={sockname}, peername={peername}")
            transport = None
            try:
                transport = do_ssh_paramiko_connect(sock, host, username, password,
                                                    host_config or ssh_lookup("*"),
                                                    keys,
                                                    paramiko_config,
                                                    auth_modes)
            except SSHAuthenticationError as e:
                log(f"paramiko authentication errors on socket {sock} with modes {auth_modes}: {e.errors}",
                    exc_info=True)
                pw_errors = []
                for errs in e.errors.values():
                    pw_errors += errs
                if ("key" in auth_modes or "agent" in auth_modes) and PARAMIKO_SESSION_LOST in pw_errors:
                    #try connecting again but without 'key' and 'agent' authentication:
                    #see https://github.com/Xpra-org/xpra/issues/3223
                    for m in ("key", "agent"):
                        try:
                            auth_modes.remove(m)
                        except KeyError:
                            pass
                    log.info(f"retrying SSH authentication with modes {csv(auth_modes)}")
                    continue
                raise
            else:
                #we have a transport!
                break
            finally:
                if sock and not transport:
                    noerr(sock.shutdown)
                    noerr(sock.close)
                    sock = None

        remote_port = display_desc.get("remote_port", 0)
        if remote_port:
            #we want to connect directly to a remote port,
            #we don't need to run a command
            chan = transport.open_channel("direct-tcpip", ("localhost", remote_port), ('localhost', 0))
            log(f"direct channel to remote port {remote_port} : {chan}")
        else:
            chan = paramiko_run_remote_xpra(transport, proxy_command, remote_xpra,
                                            socket_dir, display_as_args, paramiko_config)
        conn = SSHSocketConnection(chan, sock, sockname, peername, (host, port), socket_info)
        conn.target = host_target_string("ssh", username, host, port, display)
        conn.timeout = SOCKET_TIMEOUT
        conn.start_stderr_reader()
        return conn


#workaround incompatibility between paramiko and gssapi:
class nogssapi_context(nomodule_context):

    def __init__(self):
        super().__init__("gssapi")


def get_default_keyfiles():
    dkf = os.environ.get("XPRA_SSH_DEFAULT_KEYFILES", None)
    if dkf is not None:
        return [x for x in dkf.split(":") if x]
    return [osexpand(os.path.join("~/", ".ssh", keyfile)) for keyfile in ("id_ed25519", "id_ecdsa", "id_rsa", "id_dsa")]

AUTH_MODES = ("none", "agent", "key", "password")

def get_auth_modes(paramiko_config, host_config, password):
    def configvalue(key):
        #if the paramiko config has a setting, honour it:
        if paramiko_config and key in paramiko_config:
            return paramiko_config.get(key)
        #fallback to the value from the host config:
        return (host_config or {}).get(key)
    def configbool(key, default_value=True):
        return parse_bool(key, configvalue(key), default_value)
    auth_str = configvalue("auth")
    if auth_str:
        return auth_str.split("+")
    auth = []
    if configbool("noneauthentication", NONE_AUTH):
        auth.append("none")
    if password and configbool("passwordauthentication", PASSWORD_AUTH):
        auth.append("password")
    if configbool("agentauthentication", AGENT_AUTH):
        auth.append("agent")
    # Some people do two-factor using KEY_AUTH to kick things off, so this happens first
    if configbool("keyauthentication", KEY_AUTH):
        auth.append("key")
    if not password and configbool("passwordauthentication", PASSWORD_AUTH):
        auth.append("password")
    return auth


class iauthhandler:
    def __init__(self, password):
        self.authcount = 0
        self.password = password
    def handle_request(self, title, instructions, prompt_list):
        log("handle_request%s counter=%i", (title, instructions, prompt_list), self.authcount)
        p = []
        for pent in prompt_list:
            if self.password:
                p.append(self.password)
                self.password = None
            else:
                p.append(input_pass(pent[0]))
        self.authcount += 1
        log(f"handle_request(..) returning {len(p)} values")
        return p


def do_ssh_paramiko_connect(chan, host, username, password,
                            host_config=None, keyfiles=None, paramiko_config=None, auth_modes=AUTH_MODES):
    from paramiko import SSHException
    from paramiko.transport import Transport
    transport = Transport(chan)
    transport.use_compression(False)
    log("SSH transport %s", transport)
    try:
        transport.start_client()
    except SSHException as e:
        log("SSH negotiation failed", exc_info=True)
        raise InitExit(EXIT_SSH_FAILURE, "SSH negotiation failed: %s" % e) from None
    return do_ssh_paramiko_connect_to(transport, host, username, password,
                                      host_config, keyfiles, paramiko_config, auth_modes)

def do_ssh_paramiko_connect_to(transport, host, username, password,
                               host_config=None, keyfiles=None, paramiko_config=None, auth_modes=AUTH_MODES):
    from paramiko import SSHException, PasswordRequiredException
    from paramiko.agent import Agent
    from paramiko.hostkeys import HostKeys
    log("do_ssh_paramiko_connect_to%s", (transport, host, username, password, host_config, keyfiles, paramiko_config))

    def configvalue(key):
        #if the paramiko config has a setting, honour it:
        if paramiko_config and key in paramiko_config:
            return paramiko_config.get(key)
        #fallback to the value from the host config:
        return (host_config or {}).get(key)
    def configbool(key, default_value=True):
        return parse_bool(key, configvalue(key), default_value)
    def configint(key, default_value=0):
        v = configvalue(key)
        if v is None:
            return default_value
        return int(v)

    host_key = transport.get_remote_server_key()
    assert host_key, "no remote server key"
    log("remote_server_key=%s", keymd5(host_key))
    if configbool("verify-hostkey", VERIFY_HOSTKEY):
        host_keys = HostKeys()
        host_keys_filename = None
        KNOWN_HOSTS = get_ssh_known_hosts_files()
        for known_hosts in KNOWN_HOSTS:
            host_keys.clear()
            try:
                path = os.path.expanduser(known_hosts)
                if os.path.exists(path):
                    host_keys.load(path)
                    log("HostKeys.load(%s) successful", path)
                    host_keys_filename = path
                    break
            except IOError:
                log("HostKeys.load(%s)", known_hosts, exc_info=True)

        log("host keys=%s", host_keys)
        keys = safe_lookup(host_keys, host)
        known_host_key = (keys or {}).get(host_key.get_name())
        def keyname():
            return host_key.get_name().replace("ssh-", "")
        if known_host_key and host_key==known_host_key:
            assert host_key
            log("%s host key '%s' OK for host '%s'", keyname(), keymd5(host_key), host)
        else:
            dnscheck = ""
            if configbool("verifyhostkeydns"):
                try:
                    from xpra.net.sshfp import check_host_key
                    dnscheck = check_host_key(host, host_key)
                except ImportError as e:
                    log("verifyhostkeydns failed", exc_info=True)
                    log.info("cannot check SSHFP DNS records")
                    log.info(" %s", e)
            log("dnscheck=%s", dnscheck)
            def adddnscheckinfo(q):
                if dnscheck is not True:
                    if dnscheck:
                        q += [
                            "SSHFP validation failed:",
                            dnscheck
                            ]
                    else:
                        q += [
                            "SSHFP validation failed"
                            ]
            if dnscheck is True:
                #DNSSEC provided a matching record
                log.info("found a valid SSHFP record for host %s", host)
            elif known_host_key:
                log.warn("Warning: SSH server key mismatch")
                qinfo = [
"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!",
"IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!",
"Someone could be eavesdropping on you right now (man-in-the-middle attack)!",
"It is also possible that a host key has just been changed.",
f"The fingerprint for the {keyname()} key sent by the remote host is",
keymd5(host_key),
]
                adddnscheckinfo(qinfo)
                if configbool("stricthostkeychecking", VERIFY_STRICT):
                    log.warn("Host key verification failed.")
                    #TODO: show alert with no option to accept key
                    qinfo += [
                        "Please contact your system administrator.",
                        "Add correct host key in %s to get rid of this message.",
                        f"Offending {keyname()} key in {host_keys_filename}",
                        f"ECDSA host key for {keyname()} has changed and you have requested strict checking.",
                        ]
                    sys.stderr.write(os.linesep.join(qinfo))
                    transport.close()
                    raise InitExit(EXIT_SSH_KEY_FAILURE, "SSH Host key has changed")
                if not confirm(qinfo):
                    transport.close()
                    raise InitExit(EXIT_SSH_KEY_FAILURE, "SSH Host key has changed")
                log.info("host key confirmed")
            else:
                assert (not keys) or (host_key.get_name() not in keys)
                if not keys:
                    log.warn("Warning: unknown SSH host '%s'", host)
                else:
                    log.warn("Warning: unknown %s SSH host key", keyname())
                qinfo = [
                    f"The authenticity of host {host!r} can't be established.",
                    f"{keyname()} key fingerprint is",
                    keymd5(host_key),
                    ]
                adddnscheckinfo(qinfo)
                if not confirm(qinfo):
                    transport.close()
                    raise InitExit(EXIT_SSH_KEY_FAILURE, f"Unknown SSH host {host!r}")
                log.info("host key confirmed")
            if configbool("addkey", ADD_KEY):
                try:
                    if not host_keys_filename:
                        #the first one is the default,
                        #ie: ~/.ssh/known_hosts on posix
                        host_keys_filename = os.path.expanduser(KNOWN_HOSTS[0])
                    log(f"adding {keyname()} key for host {host!r} to {host_keys_filename!r}")
                    if not os.path.exists(host_keys_filename):
                        keys_dir = os.path.dirname(host_keys_filename)
                        if not os.path.exists(keys_dir):
                            log(f"creating keys directory {keys_dir!r}")
                            os.mkdir(keys_dir, 0o700)
                        elif not os.path.isdir(keys_dir):
                            log.warn(f"Warning: {keys_dir!r} is not a directory")
                            log.warn(" key not saved")
                        if os.path.exists(keys_dir) and os.path.isdir(keys_dir):
                            log(f"creating known host file {host_keys_filename!r}")
                            with umask_context(0o133):
                                with open(host_keys_filename, "ab+"):
                                    pass
                    host_keys.add(host, host_key.get_name(), host_key)
                    host_keys.save(host_keys_filename)
                except OSError as e:
                    log(f"failed to add key to {host_keys_filename!r}")
                    log.error(f"Error adding key to {host_keys_filename!r}")
                    log.error(f" {e}")
                except Exception as e:
                    log.error("cannot add key", exc_info=True)
    else:
        log("ssh host key verification skipped")


    auth_errors = {}

    def auth_agent():
        agent = Agent()
        agent_keys = agent.get_keys()
        log("agent keys: %s", agent_keys)
        if agent_keys:
            for agent_key in agent_keys:
                log("trying ssh-agent key '%s'", keymd5(agent_key))
                try:
                    transport.auth_publickey(username, agent_key)
                    if transport.is_authenticated():
                        log("authenticated using agent and key '%s'", keymd5(agent_key))
                        break
                except SSHException as e:
                    auth_errors.setdefault("agent", []).append(str(e))
                    log.info("SSH agent key '%s' rejected for user '%s'", keymd5(agent_key), username)
                    log("%s%s", transport.auth_publickey, (username, agent_key), exc_info=True)
                    if str(e)==PARAMIKO_SESSION_LOST:
                        #no point in trying more keys
                        break
            if not transport.is_authenticated():
                log.info("agent authentication failed, tried %i key%s", len(agent_keys), engs(agent_keys))

    def auth_publickey():
        log(f"trying public key authentication using {keyfiles}")
        for keyfile_path in keyfiles:
            if not os.path.exists(keyfile_path):
                log(f"no keyfile at {keyfile_path!r}")
                continue
            log(f"trying {keyfile_path!r}")
            key = None
            import paramiko
            try_key_formats = ()
            for kf in ("RSA", "DSS", "ECDSA", "Ed25519"):
                if keyfile_path.lower().endswith(kf.lower()):
                    try_key_formats = (kf, )
                    break
            if not try_key_formats:
                try_key_formats = ("RSA", "DSS", "ECDSA", "Ed25519")
            pkey_classname = None
            for pkey_classname in try_key_formats:
                pkey_class = getattr(paramiko, f"{pkey_classname}Key", None)
                if pkey_class is None:
                    log(f"no {pkey_classname} key type")
                    continue
                log(f"trying to load as {pkey_classname}")
                key = None
                try:
                    key = pkey_class.from_private_key_file(keyfile_path)
                    log.info(f"loaded {pkey_classname} private key from {keyfile_path!r}")
                    break
                except PasswordRequiredException as e:
                    log(f"{keyfile_path!r} keyfile requires a passphrase: {e}")
                    passphrase = input_pass(f"please enter the passphrase for {keyfile_path!r}")
                    if passphrase:
                        try:
                            key = pkey_class.from_private_key_file(keyfile_path, passphrase)
                            log.info(f"loaded {pkey_classname} private key from {keyfile_path!r}")
                        except SSHException as ke:
                            log("from_private_key_file", exc_info=True)
                            log.info(f"cannot load key from file {keyfile_path}:")
                            for emsg in str(ke).split(". "):
                                log.info(" %s.", emsg)
                    break
                except Exception:
                    log(f"auth_publickey() loading as {pkey_classname}", exc_info=True)
                    key_data = load_binary_file(keyfile_path)
                    if key_data and key_data.find(b"BEGIN OPENSSH PRIVATE KEY")>=0 and paramiko.__version__<"2.7":
                        log.warn(f"Warning: private key {keyfile_path!r}")
                        log.warn(" this file seems to be using OpenSSH's own format")
                        log.warn(" please convert it to something more standard (ie: PEM)")
                        log.warn(" so it can be used with the paramiko backend")
                        if WIN32:
                            log.warn(" or switch to the Putty Plink backend with:")
                            log.warn(" '--ssh=plink -ssh -agent'")
                        else:
                            log.warn(" or switch to the OpenSSH backend with:")
                            log.warn(" '--ssh=ssh'")
            if key:
                log(f"auth_publickey using {keyfile_path!r} as {pkey_classname}: {keymd5(key)}")
                try:
                    transport.auth_publickey(username, key)
                except SSHException as e:
                    auth_errors.setdefault("key", []).append(str(e))
                    log(f"key {keyfile_path!r} rejected", exc_info=True)
                    log.info(f"SSH authentication using key {keyfile_path!r} failed:")
                    log.info(f" {e}")
                    if str(e)==PARAMIKO_SESSION_LOST:
                        #no point in trying more keys
                        break
                else:
                    if transport.is_authenticated():
                        break
            else:
                log.error(f"Error: cannot load private key {keyfile_path!r}")

    def auth_none():
        log("trying none authentication")
        try:
            transport.auth_none(username)
        except SSHException as e:
            auth_errors.setdefault("none", []).append(str(e))
            log("auth_none()", exc_info=True)

    def auth_password():
        log("trying password authentication")
        try:
            transport.auth_password(username, password)
        except SSHException as e:
            auth_errors.setdefault("password", []).append(str(e))
            log("auth_password(..)", exc_info=True)
            emsgs = getattr(e, "message", str(e)).split(";")
        else:
            emsgs = []
        if not transport.is_authenticated():
            log.info("SSH password authentication failed:")
            for emsg in emsgs:
                log.info(f" {emsg}")
            if log.is_debug_enabled() and LOG_FAILED_CREDENTIALS:
                log.info(f" invalid username {username!r} or password {password!r}")

    def auth_interactive():
        log("trying interactive authentication")
        try:
            myiauthhandler = iauthhandler(password)
            transport.auth_interactive(username, myiauthhandler.handle_request, "")
        except SSHException as e:
            auth_errors.setdefault("interactive", []).append(str(e))
            log("auth_interactive(..)", exc_info=True)
            log.info("SSH password authentication failed:")
            for emsg in getattr(e, "message", str(e)).split(";"):
                log.info(f" {emsg}")
        finally:
            del myiauthhandler

    banner = transport.get_banner()
    if banner:
        log.info("SSH server banner:")
        for x in banner.splitlines():
            log.info(f" {x}")

    log(f"starting authentication, authentication methods: {auth_modes}")
    auth = list(auth_modes)
    # per the RFC we probably should do none first always and read off the supported
    # methods, however, the current code seems to work fine with OpenSSH
    while not transport.is_authenticated() and auth:
        a = auth.pop(0)
        log("auth=%s", a)
        if a=="none":
            auth_none()
        elif a=="agent":
            auth_agent()
        elif a=="key":
            auth_publickey()
        elif a=="password":
            auth_interactive()
            if not transport.is_authenticated():
                if password:
                    auth_password()
                else:
                    tries = configint("numberofpasswordprompts", PASSWORD_RETRY)
                    for _ in range(tries):
                        password = input_pass(f"please enter the SSH password for {username}@{host}")
                        if not password:
                            break
                        auth_password()
                        if transport.is_authenticated():
                            break
        else:
            log.warn(f"Warning: invalid authentication mechanism {a}")
        #detect session-lost problems:
        #(no point in continuing without a session)
        if auth_errors and not transport.is_authenticated():
            for err_strs in auth_errors.values():
                if PARAMIKO_SESSION_LOST in err_strs:
                    raise SSHAuthenticationError(host, auth_errors)
    if not transport.is_authenticated():
        transport.close()
        log(f"authentication errors: {auth_errors}")
        raise SSHAuthenticationError(host, auth_errors)
    return transport

class SSHAuthenticationError(InitExit):
    def __init__(self, host, errors):
        super().__init__(EXIT_CONNECTION_FAILED, f"SSH Authentication failed for {host!r}")
        self.errors = errors

def paramiko_run_test_command(transport, cmd):
    from paramiko import SSHException
    log(f"paramiko_run_test_command(transport, {cmd})")
    try:
        chan = transport.open_session(window_size=None, max_packet_size=0, timeout=60)
        chan.set_name(f"run-test:{cmd}")
    except SSHException as e:
        log("open_session", exc_info=True)
        raise InitExit(EXIT_SSH_FAILURE, f"failed to open SSH session: {e}") from None
    chan.exec_command(cmd)
    log(f"exec_command({cmd!r}) returned")
    start = monotonic()
    while not chan.exit_status_ready():
        if monotonic()-start>TEST_COMMAND_TIMEOUT:
            chan.close()
            raise InitException(f"SSH test command {cmd!r} timed out")
        log("exit status is not ready yet, sleeping")
        sleep(0.01)
    code = chan.recv_exit_status()
    log(f"exec_command({cmd!r})={code}")
    def chan_read(read_fn):
        try:
            return read_fn()
        except socket.error:
            log(f"chan_read({read_fn})", exc_info=True)
            return b""
    #don't wait too long for the data:
    chan.settimeout(EXEC_STDOUT_TIMEOUT)
    out = chan_read(chan.makefile().readlines)
    log(f"exec_command out={out!r}")
    chan.settimeout(EXEC_STDERR_TIMEOUT)
    err = chan_read(chan.makefile_stderr().readlines)
    log(f"exec_command err={err!r}")
    chan.close()
    return out, err, code


def paramiko_run_remote_xpra(transport, xpra_proxy_command=None, remote_xpra=None,
                             socket_dir=None, display_as_args=None, paramiko_config=None):
    from paramiko import SSHException
    assert remote_xpra
    log(f"will try to run xpra from: {remote_xpra}")
    def rtc(cmd):
        return paramiko_run_test_command(transport, cmd)
    def detectosname():
        #first, try a syntax that should work with any ssh server:
        r = rtc("echo %OS%")
        if r[2]==0 and r[0]:
            name = r[0][-1].rstrip("\n\r")
            log(f"echo %OS%={name!r}")
            if name!="%OS%":
                #MS Windows OS will return "Windows_NT" here
                log.info(f"ssh server OS is {name!r}")
                return name
        #this should work on all other OSes:
        r = rtc("echo $OSTYPE")
        if r[2]==0 and r[0]:
            name = r[0][-1].rstrip("\n\r")
            log(f"OSTYPE={name!r}")
            log.info(f"ssh server OS is {name!r}")
            return name
        return "unknown"
    WIN32_REGISTRY_QUERY = "REG QUERY \"HKEY_LOCAL_MACHINE\\Software\\Xpra\" /v InstallPath"
    def getexeinstallpath():
        cmd = WIN32_REGISTRY_QUERY
        if osname=="msys":
            #escape for msys shell:
            cmd = cmd.replace("/", "//")
        r = rtc(cmd)
        if r[2]!=0:
            return None
        for line in r[0]:
            qmatch = re.search(r"InstallPath\s*\w*\s*(.*)", line)
            if qmatch:
                return qmatch.group(1).rstrip("\n\r")
        return None
    osname = detectosname()
    tried = set()
    find_command = None
    for xpra_cmd in remote_xpra:
        found = False
        if osname.startswith("Windows") or osname in ("msys", "cygwin"):
            #on MS Windows,
            #always prefer the application path found in the registry:
            def winpath(p):
                if osname=="msys":
                    return p.replace("\\", "\\\\")
                if osname=="cygwin":
                    return "/cygdrive/"+p.replace(":\\", "/").replace("\\", "/")
                return p
            installpath = getexeinstallpath()
            if installpath:
                xpra_cmd = winpath(f"{installpath}\\Xpra_cmd.exe")
                found = True
            elif xpra_cmd.find("/")<0 and xpra_cmd.find("\\")<0:
                test_path = winpath(f"{DEFAULT_WIN32_INSTALL_PATH}\\{xpra_cmd}")
                cmd = f'dir "{test_path}"'
                r = rtc(cmd)
                if r[2]==0:
                    xpra_cmd = test_path
                    found = True
        if not found and not find_command and not osname.startswith("Windows"):
            if rtc("command")[2]==0:
                find_command = "command -v"
            else:
                find_command = "which"
        if not found and find_command:
            r = rtc(f"{find_command} {xpra_cmd}")
            out = r[0]
            if r[2]==0 and out:
                #use the actual path returned by 'command -v' or 'which':
                try:
                    xpra_cmd = out[-1].rstrip("\n\r ").lstrip("\t ")
                except Exception as e:
                    log(f"cannot get command from {xpra_cmd}: {e}")
                else:
                    if xpra_cmd.startswith(f"alias {xpra_cmd}="):
                        #ie: "alias xpra='xpra -d proxy'" -> "xpra -d proxy"
                        xpra_cmd = xpra_cmd.split("=", 1)[1].strip("'")
                    found = bool(xpra_cmd)
            elif xpra_cmd=="xpra" and osname in ("msys", "cygwin"):
                default_path = CYGWIN_DEFAULT_PATH if osname=="cygwin" else MSYS_DEFAULT_PATH
                if default_path:
                    #try the default system installation path
                    r = rtc(f"command -v '{default_path}'")
                    if r[2]==0:
                        xpra_cmd = default_path     #ie: "/mingw64/bin/xpra"
                        found = True
        if not found or xpra_cmd in tried:
            continue
        log(f"adding xpra_cmd={xpra_cmd!r}")
        tried.add(xpra_cmd)
        cmd = '"' + xpra_cmd + '" ' + ' '.join(shellquote(x) for x in xpra_proxy_command)
        if socket_dir:
            cmd += f" \"--socket-dir={socket_dir}\""
        if display_as_args:
            cmd += " "
            cmd += " ".join(shellquote(x) for x in display_as_args)
        log(f"cmd({xpra_proxy_command}, {display_as_args})={cmd}")

        #see https://github.com/paramiko/paramiko/issues/175
        #WINDOW_SIZE = 2097152
        log(f"trying to open SSH session, window-size={WINDOW_SIZE}, timeout={TIMEOUT}")
        try:
            chan = transport.open_session(window_size=WINDOW_SIZE, max_packet_size=0, timeout=TIMEOUT)
            chan.set_name("run-xpra")
        except SSHException as e:
            log("open_session", exc_info=True)
            raise InitExit(EXIT_SSH_FAILURE, f"failed to open SSH session: {e}") from None
        else:
            agent_option = str((paramiko_config or {}).get("agent", SSH_AGENT)) or "no"
            log(f"paramiko agent_option={agent_option}")
            if agent_option.lower() in TRUE_OPTIONS:
                log.info("paramiko SSH agent forwarding enabled")
                from paramiko.agent import AgentRequestHandler
                AgentRequestHandler(chan)
            log(f"channel exec_command({cmd!r})")
            chan.exec_command(cmd)
            return chan
    raise Exception("all SSH remote proxy commands have failed - is xpra installed on the remote host?")


def ssh_connect_failed(_message):
    #by the time ssh fails, we may have entered the gtk main loop
    #(and more than once thanks to the clipboard code..)
    if "gi.repository.Gtk" in sys.modules:
        from gi.repository import Gtk
        Gtk.main_quit()


def ssh_exec_connect_to(display_desc, opts=None, debug_cb=None, ssh_fail_cb=ssh_connect_failed):
    if not ssh_fail_cb:
        ssh_fail_cb = ssh_connect_failed
    sshpass_command = None
    try:
        cmd = list(display_desc["full_ssh"])
        kwargs = {}
        env = display_desc.get("env")
        if env is None:
            env = get_saved_env()
        if display_desc.get("is_putty"):
            #special env used by plink:
            env = os.environ.copy()
            env["PLINK_PROTOCOL"] = "ssh"
        kwargs["stderr"] = sys.stderr
        if WIN32:
            from subprocess import CREATE_NEW_PROCESS_GROUP, CREATE_NEW_CONSOLE, STARTUPINFO, STARTF_USESHOWWINDOW
            startupinfo = STARTUPINFO()
            startupinfo.dwFlags |= STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0     #aka win32.con.SW_HIDE
            flags = CREATE_NEW_PROCESS_GROUP | CREATE_NEW_CONSOLE
            kwargs.update({
                "startupinfo"   : startupinfo,
                "creationflags" : flags,
                "stderr"        : PIPE,
                })
        elif not display_desc.get("exit_ssh", False) and not OSX:
            kwargs["start_new_session"] = True
        remote_xpra = display_desc["remote_xpra"]
        assert remote_xpra
        socket_dir = display_desc.get("socket_dir")
        proxy_command = display_desc["proxy_command"]       #ie: "_proxy_start"
        display_as_args = display_desc["display_as_args"]   #ie: "--start=xterm :10"
        remote_cmd = ""
        for x in remote_xpra:
            if not remote_cmd:
                check = "if"
            else:
                check = "elif"
            if x=="xpra":
                #no absolute path, so use "command -v" to check that the command exists:
                pc = [f'{check} command -v "{x}" > /dev/null 2>&1; then']
            else:
                pc = [f'{check} [ -x {x} ]; then']
            pc += [x] + proxy_command + [shellquote(x) for x in display_as_args]
            if socket_dir:
                pc.append(f"--socket-dir={socket_dir}")
            remote_cmd += " ".join(pc)+";"
        remote_cmd += "else echo \"no run-xpra command found\"; exit 1; fi"
        if INITENV_COMMAND:
            remote_cmd = INITENV_COMMAND + ";" + remote_cmd
        #how many times we need to escape the remote command string
        #depends on how many times the ssh command is parsed
        nssh = sum(int(x=="ssh") for x in cmd)
        if nssh>=2 and MAGIC_QUOTES:
            for _ in range(nssh):
                remote_cmd = shlex.quote(remote_cmd)
        else:
            remote_cmd = f"'{remote_cmd}'"
        cmd.append(f"sh -c {remote_cmd}")
        if debug_cb:
            debug_cb(f"starting {cmd[0]} tunnel")
        #non-string arguments can make Popen choke,
        #instead of lazily converting everything to a string, we validate the command:
        for x in cmd:
            if not isinstance(x, str):
                raise InitException(f"argument is not a string: {x} ({type(x)}), found in command: {cmd}")
        password = display_desc.get("password")
        if password and not display_desc.get("is_putty", False):
            from xpra.platform.paths import get_sshpass_command
            sshpass_command = get_sshpass_command()
            if sshpass_command:
                #sshpass -e ssh ...
                cmd.insert(0, sshpass_command)
                cmd.insert(1, "-e")
                env["SSHPASS"] = password
                #the password will be used by ssh via sshpass,
                #don't try to authenticate again over the ssh-proxy connection,
                #which would trigger warnings if the server does not require
                #authentication over unix-domain-sockets:
                opts.password = None
                del display_desc["password"]

        kwargs["env"] = restore_script_env(env)

        if is_debug_enabled("ssh"):
            log.info("executing ssh command: " + " ".join(f"\"{x}\"" for x in cmd))
        child = Popen(cmd, stdin=PIPE, stdout=PIPE, **kwargs)
    except OSError as e:
        cmd_info = " ".join(repr(x) for x in cmd)
        raise InitExit(EXIT_SSH_FAILURE,
                       f"Error running ssh command {cmd_info!r}: {e}") from None
    def abort_test(action):
        """ if ssh dies, we don't need to try to read/write from its sockets """
        e = child.poll()
        if e is not None:
            had_connected = conn.input_bytecount>0 or conn.output_bytecount>0
            if had_connected:
                error_message = f"cannot {action} using SSH"
            else:
                error_message = "SSH connection failure"
            sshpass_error = None
            if sshpass_command:
                sshpass_error = {
                                 1  : "Invalid command line argument",
                                 2  : "Conflicting arguments given",
                                 3  : "General runtime error",
                                 4  : "Unrecognized response from ssh (parse error)",
                                 5  : "Invalid/incorrect password",
                                 6  : "Host public key is unknown. sshpass exits without confirming the new key.",
                                 }.get(e)
                if sshpass_error:
                    error_message += f": {sshpass_error}"
            if debug_cb:
                debug_cb(error_message)
            if ssh_fail_cb:
                ssh_fail_cb(error_message)
            if "ssh_abort" not in display_desc:
                display_desc["ssh_abort"] = True
                if not had_connected:
                    log.error("Error: SSH connection to the xpra server failed")
                    if sshpass_error:
                        log.error(f" {sshpass_error}")
                    else:
                        log.error(" check your username, hostname, display number, firewall, etc")
                    display_name = display_desc["display_name"]
                    log.error(f" for server: {display_name}")
                else:
                    log.error(f"The SSH process has terminated with exit code {e}")
                cmd_info = " ".join(display_desc["full_ssh"])
                log.error(" the command line used was:")
                log.error(f" {cmd_info}")
            raise ConnectionClosedException(error_message) from None
    def stop_tunnel():
        if POSIX:
            #on posix, the tunnel may be shared with other processes
            #so don't kill it... which may leave it behind after use.
            #but at least make sure we close all the pipes:
            for name,fd in {
                            "stdin" : child.stdin,
                            "stdout" : child.stdout,
                            "stderr" : child.stderr,
                            }.items():
                try:
                    if fd:
                        fd.close()
                except Exception as e:
                    log.error(f"Error closing ssh tunnel {name}: {e}")
            if not display_desc.get("exit_ssh", False):
                #leave it running
                return
        try:
            if child.poll() is None:
                child.terminate()
        except Exception as e:
            log.error(f"Error trying to stop ssh tunnel process: {e}")
    host = display_desc["host"]
    port = display_desc.get("port", 22)
    username = display_desc.get("username")
    display = display_desc.get("display")
    info = {
        "host"  : host,
        "port"  : port,
        }
    from xpra.net.bytestreams import TwoFileConnection
    conn = TwoFileConnection(child.stdin, child.stdout,
                             abort_test, target=(host, port),
                             socktype="ssh", close_cb=stop_tunnel, info=info)
    conn.endpoint = host_target_string("ssh", username, host, port, display)
    conn.timeout = 0            #taken care of by abort_test
    conn.process = (child, "ssh", cmd)
    if kwargs.get("stderr")==PIPE:
        def stderr_reader():
            errs = []
            while child.poll() is None:
                try:
                    v = child.stderr.readline()
                except OSError:
                    log("stderr_reader()", exc_info=True)
                    break
                if not v:
                    log(f"SSH EOF on stderr of {cmd}")
                    break
                s = bytestostr(v.rstrip(b"\n\r"))
                if s:
                    errs.append(s)
            if errs:
                log.warn("remote SSH stderr:")
                for e in errs:
                    log.warn(f" {e}")
        start_thread(stderr_reader, "ssh-stderr-reader", daemon=True)
    return conn
