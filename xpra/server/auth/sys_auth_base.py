# This file is part of Xpra.
# Copyright (C) 2013-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from collections import deque

from xpra.platform.info import get_username
from xpra.platform.dotxpra import DotXpra
from xpra.platform.paths import get_socket_dirs
from xpra.util import envint, obsc, typedict, std
from xpra.scripts.config import TRUE_OPTIONS
from xpra.net.digest import get_salt, choose_digest, verify_digest, gendigest
from xpra.os_util import hexstr, POSIX
from xpra.log import Logger
log = Logger("auth")

USED_SALT_CACHE_SIZE = envint("XPRA_USED_SALT_CACHE_SIZE", 1024*1024)
DEFAULT_UID = os.environ.get("XPRA_AUTHENTICATION_DEFAULT_UID", "nobody")
DEFAULT_GID = os.environ.get("XPRA_AUTHENTICATION_DEFAULT_GID", "nobody")


def xor(s1,s2):
    return b"".join(b"%c" % (a ^ b) for a,b in zip(s1,s2))

def parse_uid(v) -> int:
    if v:
        try:
            return int(v)
        except (TypeError, ValueError):
            log("uid '%s' is not an int", v)
    if POSIX:
        try:
            import pwd  #pylint: disable=import-outside-toplevel
            return pwd.getpwnam(v or DEFAULT_UID).pw_uid
        except Exception as e:
            log("parse_uid(%s)", v, exc_info=True)
            log.error("Error: cannot find uid of '%s': %s", v, e)
        return os.getuid()
    return -1

def parse_gid(v) -> int:
    if v:
        try:
            return int(v)
        except (TypeError, ValueError):
            log("gid '%s' is not an int", v)
    if POSIX:
        try:
            import grp          #@UnresolvedImport pylint: disable=import-outside-toplevel
            return grp.getgrnam(v or DEFAULT_GID).gr_gid
        except Exception as e:
            log("parse_gid(%s)", v, exc_info=True)
            log.error("Error: cannot find gid of '%s': %s", v, e)
        return os.getgid()
    return -1


class SysAuthenticatorBase:
    USED_SALT = deque(maxlen=USED_SALT_CACHE_SIZE)
    DEFAULT_PROMPT = "password for user '{username}'"
    CLIENT_USERNAME = False

    def __init__(self, **kwargs):
        self.username = kwargs.get("username", get_username())
        if str(kwargs.get("client-username", self.CLIENT_USERNAME)).lower() in TRUE_OPTIONS:
            #allow the client to specify the username to authenticate with:
            self.username = kwargs.get("remote", {}).get("username", self.username)
        self.salt = None
        self.digest = None
        self.salt_digest = None
        prompt_attr = {"username" : std(self.username)}
        self.prompt = kwargs.pop("prompt", self.DEFAULT_PROMPT).format(**prompt_attr)
        self.socket_dirs = kwargs.pop("socket-dirs", get_socket_dirs())
        self.challenge_sent = False
        self.passed = False
        self.password_used = None
        #we can't warn about unused options
        #because the options are shared with other socket options (nodelay, cork, etc)
        #unused = dict((k,v) for k,v in kwargs.items() if k not in ("connection", "exec_cwd", "username"))
        #if unused:
        #    log.warn("Warning: unused keyword arguments for %s authentication:", self)
        #    log.warn(" %s", unused)
        log("auth prompt=%s, socket_dirs=%s", self.prompt, self.socket_dirs)

    def get_uid(self) -> int:
        raise NotImplementedError()

    def get_gid(self) -> int:
        raise NotImplementedError()

    def requires_challenge(self) -> bool:
        return True

    def get_challenge(self, digests):
        if self.salt is not None:
            log.error("Error: authentication challenge already sent!")
            return None
        self.salt = get_salt()
        self.digest = choose_digest(digests)
        self.challenge_sent = True
        return self.salt, self.digest

    def get_passwords(self):
        p = self.get_password()     #pylint: disable=assignment-from-none
        if p is not None:
            return (p,)
        return ()

    def get_password(self):
        return None

    def check(self, _password) -> bool:
        return False

    def authenticate(self, caps : typedict) -> bool:
        r = self.do_authenticate(caps)
        if r:
            self.passed = True
            log("authentication challenge passed for %s", self)
        return r

    def do_authenticate(self, caps : typedict) -> bool:
        if self.passed:
            log("invalid state: challenge has already been passed")
            return False
        if not caps:
            log("invalid state: no capabilities")
            return False
        if not self.challenge_sent:
            log("invalid state: challenge has not been sent yet!")
            return False
        challenge_response = caps.strget("challenge_response")
        client_salt = caps.strget("challenge_client_salt")
        #challenge has been sent already for this module
        if not challenge_response:
            log("invalid state: challenge already sent but no response found!")
            return False
        return self.authenticate_check(challenge_response, client_salt)

    def choose_salt_digest(self, digest_modes) -> str:
        self.salt_digest = choose_digest(digest_modes)
        return self.salt_digest

    def get_response_salt(self, client_salt=None):
        server_salt = self.salt
        #make sure it does not get re-used:
        self.salt = None
        if client_salt is None:
            return server_salt
        salt = gendigest(self.salt_digest, client_salt, server_salt)
        if salt in SysAuthenticator.USED_SALT:
            raise Exception("danger: an attempt was made to re-use the same computed salt")
        log("combined salt(%s, %s)=%s", hexstr(server_salt), hexstr(client_salt), hexstr(salt))
        SysAuthenticator.USED_SALT.append(salt)
        return salt

    def authenticate_check(self, challenge_response : str, client_salt : str=None) -> bool:
        if self.salt is None:
            log.error("Error: illegal challenge response received - salt cleared or unset")
            return False
        salt = self.get_response_salt(client_salt)
        password = gendigest("xor", challenge_response, salt)
        log("authenticate_check(%s, %s) response salt=%s",
            obsc(repr(challenge_response)), repr(client_salt), repr(salt))
        #warning: enabling logging here would log the actual system password!
        #log.info("authenticate(%s, %s) password=%s (%s)",
        #    hexstr(challenge_response), hexstr(client_salt), password, hexstr(password))
        #verify login:
        try :
            ret = self.check(password)
            log("authenticate_check(..)=%s", ret)
        except Exception as e:
            log("check(..)", exc_info=True)
            log.error("Error: %s authentication check failed:", self)
            log.error(" %s", e)
            return False
        return ret

    def authenticate_hmac(self, challenge_response : str, client_salt : str=None) -> bool:
        log("sys_auth_base.authenticate_hmac(%r, %r)", challenge_response, client_salt)
        if not self.salt:
            log.error("Error: illegal challenge response received - salt cleared or unset")
            return None
        salt = self.get_response_salt(client_salt)
        passwords = self.get_passwords()
        if not passwords:
            log.warn("Warning: %s authentication failed", self)
            log.warn(" no password defined for '%s'", self.username)
            return False
        log("found %i passwords using %r", len(passwords), self)
        for x in passwords:
            if verify_digest(self.digest, x, salt, challenge_response):
                self.password_used = x
                return True
        log.warn("Warning: %s challenge for '%s' does not match", self.digest, self.username)
        if len(passwords)>1:
            log.warn(" checked %i passwords", len(passwords))
        return False

    def get_sessions(self):
        uid = self.get_uid()
        gid = self.get_gid()
        log("%s.get_sessions() uid=%i, gid=%i", self, uid, gid)
        try:
            sockdir = DotXpra(None, self.socket_dirs, actual_username=self.username, uid=uid, gid=gid)
            results = sockdir.sockets(check_uid=uid)
            displays = []
            for state, display in results:
                if state==DotXpra.LIVE and display not in displays:
                    displays.append(display)
            log("sockdir=%s, results=%s, displays=%s", sockdir, results, displays)
        except Exception as e:
            log("get_sessions()", exc_info=True)
            log.error("Error: cannot get the list of sessions for '%s':", self.username)
            log.error(" %s", e)
            displays = []
        v = uid, gid, displays, {}, {}
        log("%s.get_sessions()=%s", self, v)
        return v


class SysAuthenticator(SysAuthenticatorBase):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pw = None
        if POSIX:
            try:
                import pwd  #pylint: disable=import-outside-toplevel
                self.pw = pwd.getpwnam(self.username)
            except Exception:
                log("cannot load password database entry for '%s'", self.username, exc_info=True)

    def get_uid(self) -> int:
        if self.pw is None:
            raise Exception("username '%s' not found" % self.username)
        return self.pw.pw_uid

    def get_gid(self) -> int:
        if self.pw is None:
            raise Exception("username '%s' not found" % self.username)
        return self.pw.pw_gid
