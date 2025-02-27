# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2015-2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

#pylint: disable=line-too-long

import binascii

from xpra.util import csv, typedict, roundup
from xpra.log import Logger
log = Logger("encoding")

#Warning: many systems will fail above 8k because of memory constraints
# encoders can allocate many times more memory to hold the frames..
TEST_LIMIT_W, TEST_LIMIT_H = 8192, 8192

unhex = binascii.unhexlify

#this test data was generated using a 24x16 blank image as input
TEST_COMPRESSED_DATA = {
    "h264": {
        "YUV420P" : unhex("000000016764000aacb317cbc2000003000200000300651e244cd00000000168e970312c8b0000010605ffff56dc45e9bde6d948b7962cd820d923eeef78323634202d20636f726520313432202d20482e3236342f4d5045472d342041564320636f646563202d20436f70796c65667420323030332d32303134202d20687474703a2f2f7777772e766964656f6c616e2e6f72672f783236342e68746d6c202d206f7074696f6e733a2063616261633d31207265663d35206465626c6f636b3d313a303a3020616e616c7973653d3078333a3078313133206d653d756d68207375626d653d38207073793d31207073795f72643d312e30303a302e3030206d697865645f7265663d31206d655f72616e67653d3136206368726f6d615f6d653d31207472656c6c69733d31203878386463743d312063716d3d3020646561647a6f6e653d32312c313120666173745f70736b69703d31206368726f6d615f71705f6f66667365743d2d3220746872656164733d31206c6f6f6b61686561645f746872656164733d3120736c696365645f746872656164733d30206e723d3020646563696d6174653d3120696e7465726c616365643d3020626c757261795f636f6d7061743d3020636f6e73747261696e65645f696e7472613d3020626672616d65733d3020776569676874703d32206b6579696e743d393939393939206b6579696e745f6d696e3d353030303030207363656e656375743d343020696e7472615f726566726573683d302072633d637266206d62747265653d30206372663d33382e322071636f6d703d302e36302071706d696e3d302071706d61783d3639207170737465703d342069705f726174696f3d312e34302061713d313a312e3030008000000165888404bffe841fc0a667f891ea1728763fecb5e1"),
        "YUV422P" : unhex("00000001677a000abcb317cbc2000003000200000300651e244cd00000000168e970312c8b0000010605ffff56dc45e9bde6d948b7962cd820d923eeef78323634202d20636f726520313432202d20482e3236342f4d5045472d342041564320636f646563202d20436f70796c65667420323030332d32303134202d20687474703a2f2f7777772e766964656f6c616e2e6f72672f783236342e68746d6c202d206f7074696f6e733a2063616261633d31207265663d35206465626c6f636b3d313a303a3020616e616c7973653d3078333a3078313133206d653d756d68207375626d653d38207073793d31207073795f72643d312e30303a302e3030206d697865645f7265663d31206d655f72616e67653d3136206368726f6d615f6d653d31207472656c6c69733d31203878386463743d312063716d3d3020646561647a6f6e653d32312c313120666173745f70736b69703d31206368726f6d615f71705f6f66667365743d2d3220746872656164733d31206c6f6f6b61686561645f746872656164733d3120736c696365645f746872656164733d30206e723d3020646563696d6174653d3120696e7465726c616365643d3020626c757261795f636f6d7061743d3020636f6e73747261696e65645f696e7472613d3020626672616d65733d3020776569676874703d32206b6579696e743d393939393939206b6579696e745f6d696e3d353030303030207363656e656375743d343020696e7472615f726566726573683d302072633d637266206d62747265653d30206372663d33382e322071636f6d703d302e36302071706d696e3d302071706d61783d3639207170737465703d342069705f726174696f3d312e34302061713d313a312e3030008000000165888404bffe841fc0a667f891ec3d121e72aecb5f"),
        "YUV444P" : unhex("0000000167f4000a919662f89e1000000300100000030328f12266800000000168e970311121100000010605ffff55dc45e9bde6d948b7962cd820d923eeef78323634202d20636f726520313432202d20482e3236342f4d5045472d342041564320636f646563202d20436f70796c65667420323030332d32303134202d20687474703a2f2f7777772e766964656f6c616e2e6f72672f783236342e68746d6c202d206f7074696f6e733a2063616261633d31207265663d35206465626c6f636b3d313a303a3020616e616c7973653d3078333a3078313133206d653d756d68207375626d653d38207073793d31207073795f72643d312e30303a302e3030206d697865645f7265663d31206d655f72616e67653d3136206368726f6d615f6d653d31207472656c6c69733d31203878386463743d312063716d3d3020646561647a6f6e653d32312c313120666173745f70736b69703d31206368726f6d615f71705f6f66667365743d3420746872656164733d31206c6f6f6b61686561645f746872656164733d3120736c696365645f746872656164733d30206e723d3020646563696d6174653d3120696e7465726c616365643d3020626c757261795f636f6d7061743d3020636f6e73747261696e65645f696e7472613d3020626672616d65733d3020776569676874703d32206b6579696e743d393939393939206b6579696e745f6d696e3d353030303030207363656e656375743d343020696e7472615f726566726573683d302072633d637266206d62747265653d30206372663d33382e322071636f6d703d302e36302071706d696e3d302071706d61783d3639207170737465703d342069705f726174696f3d312e34302061713d313a312e3030008000000165888404bffeeb1fc0a667f75e658f9a9fccb1f341ffff"),
        },
    "vp8" : {
        "YUV420P" : unhex("1003009d012a1800100000070885858899848800281013ad501fc01fd01050122780feffbb029ffffa2546bd18c06f7ffe8951fffe8951af46301bdfffa22a00"),
        },
    "vp9" : {
        "YUV420P" : unhex("8249834200017000f60038241c18000000200000047ffffffba9da00059fffffff753b413bffffffeea7680000"),
        "YUV444P" : unhex("a249834200002e001ec007048383000000040000223fffffeea76800c7ffffffeea7680677ffffff753b40081000"),
        },
}

TEST_PICTURES = {
    "png" : (
        unhex("89504e470d0a1a0a0000000d4948445200000020000000200806000000737a7af40000002849444154785eedd08100000000c3a0f9531fe4855061c0800103060c183060c0800103060cbc0f0c102000013337932a0000000049454e44ae426082"),
        unhex("89504e470d0a1a0a0000000d4948445200000020000000200802000000fc18eda30000002549444154785eedd03101000000c2a0f54fed610d884061c0800103060c183060c080810f0c0c20000174754ae90000000049454e44ae426082"),
        ),
    "png/L" : (
        unhex("89504e470d0a1a0a0000000d4948445200000020000000200800000000561125280000000274524e5300ff5b9122b50000002049444154785e63fccf801f3011906718550009a1d170180d07e4bc323cd20300a33d013f95f841e70000000049454e44ae426082"),
        unhex("89504e470d0a1a0a0000000d4948445200000020000000200800000000561125280000001549444154785e63601805a321301a02a321803d0400042000017854be5c0000000049454e44ae426082"),
        ),
    "png/P" : (
        unhex("89504e470d0a1a0a0000000d494844520000002000000020080300000044a48ac600000300504c5445000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000b330f4880000010074524e53ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff0053f707250000001c49444154785e63f84f00308c2a0087c068384012c268388ca87000003f68fc2e077ed1070000000049454e44ae426082"),
        unhex("89504e470d0a1a0a0000000d494844520000002000000020080300000044a48ac600000300504c5445000000000000000000000000000000000000000000000000000000000000000000330000660000990000cc0000ff0000003300333300663300993300cc3300ff3300006600336600666600996600cc6600ff6600009900339900669900999900cc9900ff990000cc0033cc0066cc0099cc00cccc00ffcc0000ff0033ff0066ff0099ff00ccff00ffff00000033330033660033990033cc0033ff0033003333333333663333993333cc3333ff3333006633336633666633996633cc6633ff6633009933339933669933999933cc9933ff993300cc3333cc3366cc3399cc33cccc33ffcc3300ff3333ff3366ff3399ff33ccff33ffff33000066330066660066990066cc0066ff0066003366333366663366993366cc3366ff3366006666336666666666996666cc6666ff6666009966339966669966999966cc9966ff996600cc6633cc6666cc6699cc66cccc66ffcc6600ff6633ff6666ff6699ff66ccff66ffff66000099330099660099990099cc0099ff0099003399333399663399993399cc3399ff3399006699336699666699996699cc6699ff6699009999339999669999999999cc9999ff999900cc9933cc9966cc9999cc99cccc99ffcc9900ff9933ff9966ff9999ff99ccff99ffff990000cc3300cc6600cc9900cccc00ccff00cc0033cc3333cc6633cc9933cccc33ccff33cc0066cc3366cc6666cc9966cccc66ccff66cc0099cc3399cc6699cc9999cccc99ccff99cc00cccc33cccc66cccc99ccccccccccffcccc00ffcc33ffcc66ffcc99ffccccffccffffcc0000ff3300ff6600ff9900ffcc00ffff00ff0033ff3333ff6633ff9933ffcc33ffff33ff0066ff3366ff6666ff9966ffcc66ffff66ff0099ff3399ff6699ff9999ffcc99ffff99ff00ccff33ccff66ccff99ccffccccffffccff00ffff33ffff66ffff99ffffccffffffffff000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000023faca40000001549444154785e63601805a321301a02a321803d0400042000017854be5c0000000049454e44ae426082"),
        ),
    "jpeg" : (
        unhex("ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123251d283a333d3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffdb0043011112121815182f1a1a2f634238426363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363ffc00011080020002003012200021101031101ffc4001500010100000000000000000000000000000007ffc40014100100000000000000000000000000000000ffc40014010100000000000000000000000000000000ffc40014110100000000000000000000000000000000ffda000c03010002110311003f009f800000000000ffd9"),
        unhex("ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211101318281a181616183123251d283a333d3c3933383740485c4e404457453738506d51575f626768673e4d71797064785c656763ffdb0043011112121815182f1a1a2f634238426363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363636363ffc00011080020002003012200021101031101ffc4001500010100000000000000000000000000000007ffc40014100100000000000000000000000000000000ffc40014010100000000000000000000000000000000ffc40014110100000000000000000000000000000000ffda000c03010002110311003f009f800000000000ffd9"),
        ),
    "webp" : (
        unhex("524946465c00000057454250565038580a000000100000001f00001f0000414c50480f00000001071011110012c2ffef7a44ff530f005650382026000000d002009d012a200020003ed162aa4fa825a3a2280801001a096900003da3a000fef39d800000"),
        unhex("524946465c00000057454250565038580a000000100000001f00001f0000414c50480f00000001071011110012c2ffef7a44ff530f005650382026000000d002009d012a200020003ed162aa4fa825a3a2280801001a096900003da3a000fef39d800000"),
        ),
    }


def makebuf(size, b=0x20):
    return (chr(b).encode())*size


def make_test_image(pixel_format, w, h):
    from xpra.codecs.image_wrapper import ImageWrapper
    from xpra.codecs.codec_constants import get_subsampling_divs
    #import time
    #start = monotonic()
    if pixel_format.startswith("YUV") or pixel_format.startswith("GBRP") or pixel_format=="NV12":
        divs = get_subsampling_divs(pixel_format)
        try:
            depth = int(pixel_format.split("P")[1])   #ie: YUV444P10 -> 10
        except (IndexError, ValueError):
            depth = 8
        Bpp = roundup(depth, 8)//8
        nplanes = len(divs)
        ydiv = divs[0]  #always (1, 1)
        y = makebuf(w//ydiv[0]*h//ydiv[1]*Bpp)
        udiv = divs[1]
        u = makebuf(w//udiv[0]*h//udiv[1]*Bpp)
        planes = [y, u]
        strides = [w//ydiv[0]*Bpp, w//udiv[0]*Bpp]
        if nplanes==3:
            vdiv = divs[2]
            v = makebuf(w//vdiv[0]*h//vdiv[1]*Bpp)
            planes.append(v)
            strides.append(w//vdiv[0]*Bpp)
        image = ImageWrapper(0, 0, w, h, planes, pixel_format, 32, strides, planes=nplanes, thread_safe=True)
        #l = len(y)+len(u)+len(v)
    elif pixel_format in ("RGB", "BGR", "RGBX", "BGRX", "XRGB", "BGRA", "RGBA", "r210", "BGR48"):
        if pixel_format=="BGR48":
            stride = w*6
        else:
            stride = w*len(pixel_format)
        rgb_data = makebuf(stride*h)
        image = ImageWrapper(0, 0, w, h, rgb_data, pixel_format, 32, stride, planes=ImageWrapper.PACKED, thread_safe=True)
        #l = len(rgb_data)
    else:
        raise Exception("don't know how to create a %s image" % pixel_format)
    #log("make_test_image%30s took %3ims for %6iMBytes",
    #    (pixel_format, w, h), 1000*(monotonic()-start), l//1024//1024)
    return image


def testdecoder(decoder_module, full):
    codecs = list(decoder_module.get_encodings())
    for encoding in tuple(codecs):
        try:
            testdecoding(decoder_module, encoding, full)
        except Exception as e:
            log("%s: %s decoding failed", decoder_module.get_type(), encoding, exc_info=True)
            log.warn("%s: %s decoding failed: %s", decoder_module.get_type(), encoding, e)
            del e
            codecs.remove(encoding)
    if not codecs:
        log.error("%s: all the codecs have failed! (%s)",
                  decoder_module.get_type(), csv(decoder_module.get_encodings()))
    return tuple(codecs)

def testdecoding(decoder_module, encoding, full):
    W = 24
    H = 16
    test_data_set = TEST_COMPRESSED_DATA.get(encoding)
    if not test_data_set:
        log("%s: no test data for %s", decoder_module.get_type(), encoding)
        return
    for cs in decoder_module.get_input_colorspaces(encoding):
        e = decoder_module.Decoder()
        try:
            e.init_context(encoding, W, H, cs)
            test_data = test_data_set.get(cs)
            if test_data:
                log("%s: testing %s / %s with %s bytes of data",
                    decoder_module.get_type(), encoding, cs, len(test_data))
                image = e.decompress_image(test_data)
                assert image is not None, "failed to decode test data for encoding '%s' with colorspace '%s'" % (encoding, cs)
                assert image.get_width()==W, "expected image of width %s but got %s" % (W, image.get_width())
                assert image.get_height()==H, "expected image of height %s but got %s" % (H, image.get_height())
            if full:
                log("%s: testing %s / %s with junk data", decoder_module.get_type(), encoding, cs)
                #test failures:
                try:
                    image = e.decompress_image(b"junk")
                except Exception:
                    image = None
                if image is not None:
                    raise Exception("decoding junk with %s should have failed, got %s instead" % (decoder_module.get_type(), image))
        finally:
            e.clean()


def testencoder(encoder_module, full):
    codecs = list(encoder_module.get_encodings())
    for encoding in tuple(codecs):
        try:
            testencoding(encoder_module, encoding, full)
        except Exception as e:
            log("%s: %s encoding failed", encoder_module.get_type(), encoding, exc_info=True)
            log.warn("Warning: %s encoder testing failed with %s:",
                     encoder_module.get_type(), encoding)
            log.warn(" %s", e)
            del e
            codecs.remove(encoding)
    if not codecs:
        log.error("%s: all the codecs have failed! (%s)",
                  encoder_module.get_type(), csv(encoder_module.get_encodings()))
    return tuple(codecs)

def testencoding(encoder_module, encoding, full):
    #test a bit bigger so we exercise more code:
    W = 64
    H = 32
    do_testencoding(encoder_module, encoding, W, H, full)

def get_encoder_max_sizes(encoder_module):
    w, h = TEST_LIMIT_W, TEST_LIMIT_H
    for encoding in encoder_module.get_encodings():
        ew, eh = get_encoder_max_size(encoder_module, encoding)
        w = min(w, ew)
        h = min(h, eh)
    return w, h

def get_encoder_max_size(encoder_module, encoding, limit_w=TEST_LIMIT_W, limit_h=TEST_LIMIT_H):
    #probe to find the max dimensions:
    #(it may go higher but we don't care as windows can't)
    def einfo():
        return "%s %s %s" % (encoder_module.get_type(), encoding, encoder_module.get_version())
    log("get_encoder_max_size%s", (encoder_module, encoding, limit_w, limit_h))
    maxw = w = 512
    while w<=limit_w:
        try:
            do_testencoding(encoder_module, encoding, w, 128)
            maxw = w
            w *= 2
        except Exception as e:
            log("%s is limited to max width=%i for %s:", einfo(), maxw, encoding)
            log(" %s", e)
            del e
            break
    log("%s max width=%i", einfo(), maxw)
    maxh = h = 512
    while h<=limit_h:
        try:
            do_testencoding(encoder_module, encoding, 128, h)
            maxh = h
            h *= 2
        except Exception as e:
            log("%s is limited to max height=%i for %s:", einfo(), maxh, encoding)
            log(" %s", e)
            del e
            break
    log("%s max height=%i", einfo(), maxh)
    #now try combining width and height
    #as there might be a lower limit based on the total number of pixels:
    MAX_WIDTH, MAX_HEIGHT = maxw, maxh
    #start at half:
    v = max(512, min(maxw, maxh)//2)
    while v<max(limit_w, limit_h):
        for tw, th in ((v, v), (v*2, v)):
            if tw>limit_w or th>limit_h:
                continue
            try:
                w = min(maxw, tw)
                h = min(maxh, th)
                do_testencoding(encoder_module, encoding, w, h)
                log("%s can handle %ix%i for %s", einfo(), w, h, encoding)
                MAX_WIDTH, MAX_HEIGHT = w, h
            except Exception as e:
                log("%s is limited to %ix%i for %s", einfo(), MAX_WIDTH, MAX_HEIGHT, encoding)
                log(" %s", e)
                del e
                break
        v *= 2
    log("%s max dimensions for %s: %ix%i", einfo(), encoding, MAX_WIDTH, MAX_HEIGHT)
    return MAX_WIDTH, MAX_HEIGHT


def do_testencoding(encoder_module, encoding, W, H, full=False, limit_w=TEST_LIMIT_W, limit_h=TEST_LIMIT_H):
    for cs_in in encoder_module.get_input_colorspaces(encoding):
        for cs_out in encoder_module.get_output_colorspaces(encoding, cs_in):
            e = encoder_module.Encoder()
            try:
                options = typedict({
                    "b-frames" : True,
                    "dst-formats" : [cs_out],
                    "quality" : 50,
                    "speed" : 50,
                    })
                e.init_context(encoding, W, H, cs_in, options)
                for i in range(2):
                    image = make_test_image(cs_in, W, H)
                    v = e.compress_image(image)
                    if v is None:
                        raise Exception("%s compression failed" % encoding)
                    data, meta = v
                    if not data:
                        delayed = meta.get("delayed", 0)
                        assert delayed>0, "data is empty and there are no delayed frames!"
                        if i>0:
                            #now we should get one:
                            data, meta = e.flush(delayed)
                del image
                assert data is not None, "None data for %s using %s encoding with %s / %s" % (encoder_module.get_type(), encoding, cs_in, cs_out)
                assert data, "no compressed data for %s using %s encoding with %s / %s" % (encoder_module.get_type(), encoding, cs_in, cs_out)
                assert meta is not None, "missing metadata for %s using %s encoding with %s / %s" % (encoder_module.get_type(), encoding, cs_in, cs_out)
                log("%s: %s / %s / %s passed", encoder_module, encoding, cs_in, cs_out)
                #print("test_encoder: %s.compress_image(%s)=%s" % (encoder_module.get_type(), image, (data, meta)))
                #print("compressed data with %s: %s bytes (%s), metadata: %s" % (encoder_module.get_type(), len(data), type(data), meta))
                #print("compressed data(%s, %s)=%s" % (encoding, cs_in, binascii.hexlify(data)))
                if full:
                    wrong_formats = [x for x in ("YUV420P", "YUV444P", "BGRX", "r210") if x!=cs_in]
                    #log("wrong formats (not %s): %s", cs_in, wrong_formats)
                    if wrong_formats:
                        wrong_format = wrong_formats[0]
                        try:
                            image = make_test_image(wrong_format, W, H)
                            out = e.compress_image(None, image, options=options)
                        except Exception:
                            out = None
                        assert out is None, "encoder %s should have failed using %s encoding with %s instead of %s / %s" % (encoder_module.get_type(), encoding, wrong_format, cs_in, cs_out)
                    for w,h in ((W//2, H//2), (W*2, H//2), (W//2, H**2)):
                        if w>limit_w or h>limit_h:
                            continue
                        try:
                            image = make_test_image(cs_in, w, h)
                            out = e.compress_image(None, image, options=options)
                        except Exception:
                            out = None
                        assert out is None, "encoder %s, info=%s should have failed using %s encoding with invalid size %ix%i vs %ix%i" % (encoder_module.get_type(), e.get_info(), encoding, w, h, W, H)
            finally:
                e.clean()


def testcsc(csc_module, scaling=True, full=False, test_cs_in=None, test_cs_out=None):
    W = 48
    H = 32
    log("test_csc(%s, %s, %s, %s)", csc_module, full, test_cs_in, test_cs_out)
    do_testcsc(csc_module, W, H, W, H, full, test_cs_in, test_cs_out)
    if full and scaling:
        do_testcsc(csc_module, W, H, W*2, H*2, full, test_cs_in, test_cs_out)
        do_testcsc(csc_module, W, H, W//2, H//2, full, test_cs_in, test_cs_out)

def get_csc_max_size(colorspace_converter, test_cs_in=None, test_cs_out=None, limit_w=TEST_LIMIT_W, limit_h=TEST_LIMIT_H):
    #probe to find the max dimensions:
    #(it may go higher but we don't care as windows can't)
    MAX_WIDTH, MAX_HEIGHT = 512, 512
    #as there might be a lower limit based on the total number of pixels:
    v = 512
    while v<=min(limit_w, limit_h):
        for tw, th in ((v, v), (v*2, v)):
            if tw>limit_w or th>limit_h:
                break
            try:
                do_testcsc(colorspace_converter, tw, th, tw, th, False, test_cs_in, test_cs_out, limit_w, limit_h)
                log("%s can handle %ix%i", colorspace_converter, tw, th)
                MAX_WIDTH, MAX_HEIGHT = tw, th
            except Exception:
                log("%s is limited to %ix%i for %s",
                    colorspace_converter, MAX_WIDTH, MAX_HEIGHT, (test_cs_in, test_cs_out), exc_info=True)
                break
        v *= 2
    log("%s max dimensions: %ix%i", colorspace_converter, MAX_WIDTH, MAX_HEIGHT)
    return MAX_WIDTH, MAX_HEIGHT


def do_testcsc(csc_module, iw, ih, ow, oh, full=False, test_cs_in=None, test_cs_out=None, limit_w=TEST_LIMIT_W, limit_h=TEST_LIMIT_H):
    log("do_testcsc%s", (csc_module, iw, ih, ow, oh, full, test_cs_in, test_cs_out, TEST_LIMIT_W, TEST_LIMIT_H))
    cs_in_list = test_cs_in
    if cs_in_list is None:
        cs_in_list = csc_module.get_input_colorspaces()
    for cs_in in cs_in_list:
        cs_out_list = test_cs_out
        if cs_out_list is None:
            cs_out_list = csc_module.get_output_colorspaces(cs_in)
        for cs_out in cs_out_list:
            log("%s: testing %s / %s", csc_module.get_type(), cs_in, cs_out)
            e = csc_module.ColorspaceConverter()
            try:
                e.init_context(iw, ih, cs_in, ow, oh, cs_out)
                image = make_test_image(cs_in, iw, ih)
                out = e.convert_image(image)
                #print("convert_image(%s)=%s" % (image, out))
                assert out.get_width()==ow, "expected image of width %s but got %s" % (ow, out.get_width())
                assert out.get_height()==oh, "expected image of height %s but got %s" % (oh, out.get_height())
                assert out.get_pixel_format()==cs_out, "expected pixel format %s but got %s" % (cs_out, out.get_pixel_format())
                if full:
                    for w,h in ((iw*2, ih//2), (iw//2, ih**2)):
                        if w>limit_w or h>limit_h:
                            continue
                        try:
                            image = make_test_image(cs_in, w, h)
                            out = e.convert_image(image)
                        except Exception:
                            out = None
                        if out is not None:
                            raise Exception("converting an image of a smaller size with %s should have failed, got %s instead" % (csc_module.get_type(), out))
            finally:
                e.clean()
