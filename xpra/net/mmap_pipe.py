# This file is part of Xpra.
# Copyright (C) 2011-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
from ctypes import c_ubyte, c_char, c_uint32

from xpra.util import roundup
from xpra.os_util import memoryview_to_bytes, shellsub, get_group_id, WIN32, POSIX
from xpra.scripts.config import FALSE_OPTIONS, TRUE_OPTIONS
from xpra.simple_stats import std_unit
from xpra.log import Logger

log = Logger("mmap")

MMAP_GROUP = os.environ.get("XPRA_MMAP_GROUP", "xpra")


"""
Utility functions for communicating via mmap
"""

def get_socket_group(socket_filename) -> int:
    if isinstance(socket_filename, str) and os.path.exists(socket_filename):
        s = os.stat(socket_filename)
        return s.st_gid
    log.warn("Warning: missing valid socket filename to set mmap group")
    return -1

def xpra_group() -> int:
    if POSIX:
        try:
            groups = os.getgroups()
            group_id = get_group_id(MMAP_GROUP)
            log("xpra_group() group(%s)=%s, groups=%s", MMAP_GROUP, group_id, groups)
            if group_id and group_id in groups:
                return group_id
        except Exception:
            log("xpra_group()", exc_info=True)
    return 0


def init_client_mmap(mmap_group=None, socket_filename=None, size=128*1024*1024, filename=None):
    """
        Initializes an mmap area, writes the token in it and returns:
            (success flag, mmap_area, mmap_size, temp_file, mmap_filename)
        The caller must keep hold of temp_file to ensure it does not get deleted!
        This is used by the client.
    """
    def rerr():
        return False, False, None, 0, None, None
    log("init_mmap%s", (mmap_group, socket_filename, size, filename))
    mmap_filename = filename
    mmap_temp_file = None
    delete = True
    def validate_size(size : int):
        assert size>=64*1024*1024, "mmap size is too small: %sB (minimum is 64MB)" % std_unit(size)
        assert size<=4*1024*1024*1024, "mmap is too big: %sB (maximum is 4GB)" % std_unit(size)
    try:
        import mmap
        unit = max(4096, mmap.PAGESIZE)
        #add 8 bytes for the mmap area control header zone:
        mmap_size = roundup(size + 8, unit)
        if WIN32:
            validate_size(mmap_size)
            if not filename:
                from xpra.os_util import get_hex_uuid
                filename = "xpra-%s" % get_hex_uuid()
            mmap_filename = filename
            mmap_area = mmap.mmap(0, mmap_size, filename)
            #not a real file:
            delete = False
        else:
            assert POSIX
            if filename:
                if os.path.exists(filename):
                    fd = os.open(filename, os.O_EXCL | os.O_RDWR)
                    mmap_size = os.path.getsize(mmap_filename)
                    validate_size(mmap_size)
                    #mmap_size = 4*1024*1024    #size restriction needed with ivshmem
                    delete = False
                    log.info("Using existing mmap file '%s': %sMB", mmap_filename, mmap_size//1024//1024)
                else:
                    validate_size(mmap_size)
                    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                    try:
                        fd = os.open(filename, flags)
                        mmap_temp_file = None   #os.fdopen(fd, 'w')
                        mmap_filename = filename
                    except FileExistsError:
                        log.error("Error: the mmap file '%s' already exists", filename)
                        return rerr()
            else:
                validate_size(mmap_size)
                import tempfile
                from xpra.platform.paths import get_mmap_dir
                mmap_dir = get_mmap_dir()
                subs = os.environ.copy()
                subs.update({
                    "UID"               : os.getuid(),
                    "GID"               : os.getgid(),
                    "PID"               : os.getpid(),
                    })
                mmap_dir = shellsub(mmap_dir, subs)
                if mmap_dir and not os.path.exists(mmap_dir):
                    os.mkdir(mmap_dir, 0o700)
                if not mmap_dir or not os.path.exists(mmap_dir):
                    raise Exception("mmap directory %s does not exist!" % mmap_dir)
                #create the mmap file, the mkstemp that is called via NamedTemporaryFile ensures
                #that the file is readable and writable only by the creating user ID
                try:
                    temp = tempfile.NamedTemporaryFile(prefix="xpra.", suffix=".mmap", dir=mmap_dir)
                except OSError as e:
                    log.error("Error: cannot create mmap file:")
                    log.error(" %s", e)
                    return rerr()
                #keep a reference to it so it does not disappear!
                mmap_temp_file = temp
                mmap_filename = temp.name
                fd = temp.file.fileno()
            #set the group permissions and gid if the mmap-group option is specified
            mmap_group = (mmap_group or "")
            if POSIX and mmap_group and mmap_group not in FALSE_OPTIONS:
                group_id = None
                if mmap_group=="SOCKET":
                    group_id = get_socket_group(socket_filename)
                elif mmap_group.lower()=="auto":
                    group_id = xpra_group()
                    if not group_id and socket_filename:
                        group_id = get_socket_group(socket_filename)
                elif mmap_group.lower() in TRUE_OPTIONS:
                    log.info("parsing legacy mmap-group value '%s' as 'auto'", mmap_group)
                    log.info(" please update your configuration")
                    group_id = xpra_group() or get_socket_group(socket_filename)
                else:
                    group_id = get_group_id(mmap_group)
                if group_id>0:
                    log("setting mmap file %s to group id=%i", mmap_filename, group_id)
                    try:
                        os.fchown(fd, -1, group_id)
                    except OSError as e:
                        log("fchown(%i, %i, %i) on %s", fd, -1, group_id, mmap_filename, exc_info=True)
                        log.error("Error: failed to change group ownership of mmap file to '%s':", mmap_group)
                        log.error(" %s", e)
                    from stat import S_IRUSR,S_IWUSR,S_IRGRP,S_IWGRP
                    os.fchmod(fd, S_IRUSR|S_IWUSR|S_IRGRP|S_IWGRP)
            log("using mmap file %s, fd=%s, size=%s", mmap_filename, fd, mmap_size)
            os.lseek(fd, mmap_size-1, os.SEEK_SET)
            assert os.write(fd, b'\x00')
            os.lseek(fd, 0, os.SEEK_SET)
            mmap_area = mmap.mmap(fd, length=mmap_size)
        return True, delete, mmap_area, mmap_size, mmap_temp_file, mmap_filename
    except Exception as e:
        log("failed to setup mmap: %s", e, exc_info=True)
        log.error("Error: mmap setup failed:")
        log.error(" %s", e)
        if delete:
            if mmap_temp_file:
                try:
                    mmap_temp_file.close()
                except OSError:
                    log("%s()", mmap_temp_file.close, exc_info=True)
                    log.error(" failed to remove the mmap temp file: %s", e)
            else:
                clean_mmap(mmap_filename)
        return rerr()

def clean_mmap(mmap_filename):
    log("clean_mmap(%s)", mmap_filename)
    if mmap_filename and os.path.exists(mmap_filename):
        try:
            os.unlink(mmap_filename)
        except OSError as e:
            log.error("Error: failed to remove the mmap file '%s':", mmap_filename)
            log.error(" %s", e)

DEFAULT_TOKEN_BYTES = 128

def write_mmap_token(mmap_area, token, index, count=DEFAULT_TOKEN_BYTES):
    assert count>0
    #write the token one byte at a time - no endianness
    log("write_mmap_token(%s, %#x, %#x, %#x)", mmap_area, token, index, count)
    v = token
    for i in range(0, count):
        poke = c_ubyte.from_buffer(mmap_area, index+i)
        poke.value = v % 256
        v = v>>8
    assert v==0, "token value is too big"

def read_mmap_token(mmap_area, index, count=DEFAULT_TOKEN_BYTES):
    assert count>0
    v = 0
    for i in range(0, count):
        v = v<<8
        peek = c_ubyte.from_buffer(mmap_area, index+count-1-i)
        v += peek.value
    log("read_mmap_token(%s, %#x, %#x)=%#x", mmap_area, index, count, v)
    return v


def init_server_mmap(mmap_filename, mmap_size=0):
    """
        Reads the mmap file provided by the client
        and verifies the token if supplied.
        Returns the mmap object and its size: (mmap, size)
    """
    if not WIN32:
        try:
            f = open(mmap_filename, "r+b")
        except Exception as e:
            log.error(f"Error: cannot access mmap file {mmap_filename!r}:")
            log.estr(e)
            log.error(" see mmap-group option?")
            return None, 0

    mmap_area = None
    try:
        import mmap
        if not WIN32:
            actual_mmap_size = os.path.getsize(mmap_filename)
            if mmap_size and actual_mmap_size!=mmap_size:
                log.warn("Warning: expected mmap file '%s' of size %i but got %i",
                         mmap_filename, mmap_size, actual_mmap_size)
            mmap_area = mmap.mmap(f.fileno(), mmap_size)
        else:
            if mmap_size==0:
                log.error("Error: client did not supply the mmap area size")
                log.error(" try updating your client version?")
            mmap_area = mmap.mmap(0, mmap_size, mmap_filename)
            actual_mmap_size = mmap_size
        f.close()
        return mmap_area, actual_mmap_size
    except Exception as e:
        log.error("cannot use mmap file '%s': %s", mmap_filename, e, exc_info=True)
        if mmap_area:
            mmap_area.close()
        return None, 0

def int_from_buffer(mmap_area, pos):
    return c_uint32.from_buffer(mmap_area, pos)      #@UndefinedVariable


#descr_data is a list of (offset, length)
#areas from the mmap region
def mmap_read(mmap_area, *descr_data):
    """
        Reads data from the mmap_area as written by 'mmap_write'.
        The descr_data is the list of mmap chunks used.
    """
    data_start = int_from_buffer(mmap_area, 0)
    if len(descr_data)==1:
        #construct an array directly from the mmap zone:
        offset, length = descr_data[0]
        arraytype = c_char * length
        data_start.value = offset+length
        return arraytype.from_buffer(mmap_area, offset)
    #re-construct the buffer from discontiguous chunks:
    data = []
    for offset, length in descr_data:
        mmap_area.seek(offset)
        data.append(mmap_area.read(length))
        data_start.value = offset+length
    return b"".join(data)


def mmap_write(mmap_area, mmap_size, data):
    """
        Sends 'data' to the client via the mmap shared memory region,
        returns the chunks of the mmap area used (or None if it failed)
        and the mmap area's free memory.
    """
    #This is best explained using diagrams:
    #mmap_area=[&S&E-------------data-------------]
    #The first pair of 4 bytes are occupied by:
    #S=data_start index is only updated by the client and tells us where it has read up to
    #E=data_end index is only updated here and marks where we have written up to (matches current seek)
    # '-' denotes unused/available space
    # '+' is for data we have written
    # '*' is for data we have just written in this call
    # E and S show the location pointed to by data_start/data_end
    mmap_data_start = int_from_buffer(mmap_area, 0)
    mmap_data_end = int_from_buffer(mmap_area, 4)
    start = max(8, mmap_data_start.value)
    end = max(8, mmap_data_end.value)
    l = len(data)
    log("mmap: start=%i, end=%i, size of data to write=%i", start, end, l)
    if end<start:
        #we have wrapped around but the client hasn't yet:
        #[++++++++E--------------------S+++++]
        #so there is one chunk available (from E to S) which we will use:
        #[++++++++************E--------S+++++]
        available = start-end
        chunk = available
    else:
        #we have not wrapped around yet, or the client has wrapped around too:
        #[------------S++++++++++++E---------]
        #so there are two chunks available (from E to the end, from the start to S):
        #[****--------S++++++++++++E*********]
        chunk = mmap_size-end
        available = chunk+(start-8)
    #update global mmap stats:
    mmap_free_size = available-l
    if l>(mmap_size-8):
        log.warn("Warning: mmap area is too small!")
        log.warn(" we need to store %s bytes but the mmap area is limited to %i", l, (mmap_size-8))
        return None, mmap_free_size
    if mmap_free_size<=0:
        log.warn("Warning: mmap area is full!")
        log.warn(" we need to store %s bytes but only have %s free space left", l, available)
        return None, mmap_free_size
    if l<chunk:
        # data fits in the first chunk:
        #ie: initially:
        #[----------------------------------]
        #[*********E------------------------]
        #or if data already existed:
        #[+++++++++E------------------------]
        #[+++++++++**********E--------------]
        mmap_area.seek(end)
        mmap_area.write(memoryview_to_bytes(data))
        chunks = [(end, l)]
        mmap_data_end.value = end+l
    else:
        # data does not fit in first chunk alone:
        if available>=(mmap_size/2) and available>=(l*3) and l<(start-8):
            # still plenty of free space, don't wrap around: just start again:
            #[------------------S+++++++++E------]
            #[*******E----------S+++++++++-------]
            mmap_area.seek(8)
            mmap_area.write(memoryview_to_bytes(data))
            chunks = [(8, l)]
            mmap_data_end.value = 8+l
        else:
            # split in 2 chunks: wrap around the end of the mmap buffer:
            #[------------------S+++++++++E------]
            #[******E-----------S+++++++++*******]
            mmap_area.seek(end)
            mmap_area.write(memoryview_to_bytes(data[:chunk]))
            mmap_area.seek(8)
            mmap_area.write(memoryview_to_bytes(data[chunk:]))
            l2 = l-chunk
            chunks = [(end, chunk), (8, l2)]
            mmap_data_end.value = 8+l2
    log("sending damage with mmap: %s", data)
    return chunks, mmap_free_size
