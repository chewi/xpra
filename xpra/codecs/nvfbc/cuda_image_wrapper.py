# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2018-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from time import monotonic

from xpra.codecs.nv_util import numpy_import_lock
from xpra.codecs.image_wrapper import ImageWrapper
from xpra.log import Logger

log = Logger("cuda", "nvfbc")

with numpy_import_lock:
    import numpy
    from pycuda import driver       #@UnresolvedImport


class CUDAImageWrapper(ImageWrapper):

    def __init__(self, *args):
        super().__init__(*args)
        self.stream = None
        self.cuda_device_buffer = None
        self.cuda_context = None
        self.buffer_size = 0

    def wait_for_stream(self):
        s = self.stream
        if s and not s.is_done():
            self.stream.synchronize()


    def may_download(self):
        ctx = self.cuda_context
        if self.pixels is not None or not ctx or self.freed:
            return
        assert self.cuda_device_buffer, "bug: no device buffer"
        start = monotonic()
        ctx.push()
        host_buffer = driver.pagelocked_empty(self.buffer_size, dtype=numpy.byte)   #pylint: disable=no-member
        driver.memcpy_dtoh_async(host_buffer, self.cuda_device_buffer, self.stream) #pylint: disable=no-member
        self.wait_for_stream()
        self.pixels = host_buffer.tobytes()
        elapsed = monotonic()-start
        log("may_download() from %#x to %s, size=%s, elapsed=%ims - %iMB/s",
            int(self.cuda_device_buffer), host_buffer, self.buffer_size,
            int(1000*elapsed), self.buffer_size/elapsed/1024/1024)
        self.free_cuda()
        ctx.pop()

    def freeze(self):
        #this image is already a copy when we get it
        return True

    def get_gpu_buffer(self):
        self.wait_for_stream()
        return self.cuda_device_buffer

    def has_pixels(self):
        return self.pixels is not None

    def get_pixels(self):
        self.may_download()
        return super().get_pixels()

    def clone_pixel_data(self):
        self.may_download()
        return super().clone_pixel_data()

    def get_sub_image(self, x, y, w, h):
        self.may_download()
        return super().get_sub_image(x, y, w, h)

    def free_cuda_device_buffer(self):
        cdb = self.cuda_device_buffer
        if not cdb:
            return
        log("%s.free_cuda() cuda_device_buffer=%#x", self, int(cdb or 0))
        self.cuda_device_buffer = None
        cdb.free()

    def free_cuda(self):
        self.free_cuda_device_buffer()
        self.stream = None
        self.cuda_context = None
        self.buffer_size = 0

    def free(self):
        self.free_cuda()
        return super().free()

    def clean(self):
        try:
            self.wait_for_stream()
        except driver.LogicError:  #pylint: disable=no-member
            log("%s.clean()", self, exc_info=True)
        self.free()
