from . import Image, ImageFile, _webp
from io import BytesIO


_VALID_WEBP_MODES = {
    "RGB": True,
    "RGBA": True,
    }

_VP8_MODES_BY_IDENTIFIER = {
    b"VP8 ": "RGB",
    b"VP8X": "RGBA",
    b"VP8L": "RGBA",  # lossless
    }


def _accept(prefix):
    is_riff_file_format = prefix[:4] == b"RIFF"
    is_webp_file = prefix[8:12] == b"WEBP"
    is_valid_vp8_mode = prefix[12:16] in _VP8_MODES_BY_IDENTIFIER

    return is_riff_file_format and is_webp_file and is_valid_vp8_mode


class WebPImageFile(ImageFile.ImageFile):

    format = "WEBP"
    format_description = "WebP image"

    def _open(self):
        if not _webp.HAVE_WEBPMUX:
            # Legacy mode
            data, width, height, self.mode = _webp.WebPDecode(self.fp.read())
            self.size = width, height
            self.fp = BytesIO(data)
            self.tile = [("raw", (0, 0) + self.size, 0, self.mode)]
            return

        # Use the newer AnimDecoder API to parse the (possibly) animated file,
        # and access muxed chunks like ICC/EXIF/XMP.
        self._decoder = _webp.WebPAnimDecoder(self.fp.read())

        # Get info from decoder
        width, height, loop_count, bgcolor, frame_count, mode = self._decoder.get_info()
        self.size = width, height
        self.info["loop"] = loop_count
        bg_a, bg_r, bg_g, bg_b = \
            (bgcolor >> 24) & 0xFF, \
            (bgcolor >> 16) & 0xFF, \
            (bgcolor >> 8) & 0xFF, \
            bgcolor & 0xFF
        self.info["background"] = (bg_r, bg_g, bg_b, bg_a)
        self._n_frames = frame_count
        self.mode = mode
        self.tile = []

        # Attempt to read ICC / EXIF / XMP chunks from file
        icc_profile = self._decoder.get_chunk("ICCP")
        exif = self._decoder.get_chunk("EXIF")
        xmp = self._decoder.get_chunk("XMP ")
        if icc_profile:
            self.info["icc_profile"] = icc_profile
        if exif:
            self.info["exif"] = exif
        if xmp:
            self.info["xmp"] = xmp

        # Initialize seek state
        self._reset(reset=False)
        self.seek(0)

    def _getexif(self):
        from .JpegImagePlugin import _getexif
        return _getexif(self)

    @property
    def n_frames(self):
        if not _webp.HAVE_WEBPMUX:
            return 1
        return self._n_frames

    @property
    def is_animated(self):
        if not _webp.HAVE_WEBPMUX:
            return False
        return self._n_frames > 1

    def seek(self, frame):
        # Perform some simple checks first
        if frame >= self._n_frames:
            raise EOFError("attempted to seek beyond end of sequence")
        if frame < 0:
            raise EOFError("negative frame index is not valid")

        # Set logical frame to requested position
        self.__logical_frame = frame

    def _reset(self, reset=True):
        if reset:
            self._decoder.reset()
        self.__physical_frame = 0
        self.__loaded = -1
        self.__timestamp = 0

    def _get_next(self):
        # Get next frame
        ret = self._decoder.get_next()
        self.__physical_frame += 1

        # Check if an error occurred
        if ret is None:
            self._reset() # Reset just to be safe
            self.seek(0)
            raise EOFError("failed to decode next frame in WebP file")

        # Compute duration
        data, timestamp = ret
        duration = timestamp - self.__timestamp
        self.__timestamp = timestamp
        timestamp -= duration # libwebp gives frame end, adjust to start of frame
        return data, timestamp, duration

    def _seek(self, frame):
        if self.__physical_frame == frame:
            return # Nothing to do

        if frame < self.__physical_frame:
            # Rewind to beginning
            self._reset()

        # Advance to the requested frame
        while self.__physical_frame < frame:
            self._get_next()

    def load(self):
        if self.__loaded != self.__logical_frame:
            self._seek(self.__logical_frame)

            # We need to load the image data for this frame
            data, timestamp, duration = self._get_next()
            self.info["timestamp"] = timestamp
            self.info["duration"] = duration
            self.__loaded = self.__logical_frame

            # Set tile
            self.fp = BytesIO(data)
            self.tile = [("raw", (0, 0) + self.size, 0, self.mode)]

        return super(WebPImageFile, self).load()

    def tell(self):
        return self.__logical_frame

def _save_all(im, fp, filename):
    if not _webp.HAVE_WEBPMUX:
        _save(im, fp, filename)
        return

    encoderinfo = im.encoderinfo.copy()
    append_images = encoderinfo.get("append_images", [])
    background = encoderinfo.get("background", (0, 0, 0, 0))
    duration = im.encoderinfo.get("duration", 0)
    loop = im.encoderinfo.get("loop", 0)
    minimize_size = im.encoderinfo.get("minimize_size", False)
    kmin = im.encoderinfo.get("kmin", None)
    kmax = im.encoderinfo.get("kmax", None)
    allow_mixed = im.encoderinfo.get("allow_mixed", False)
    verbose = False
    lossless = im.encoderinfo.get("lossless", False)
    quality = im.encoderinfo.get("quality", 80)
    method = im.encoderinfo.get("method", 0)
    icc_profile = im.encoderinfo.get("icc_profile", "")
    exif = im.encoderinfo.get("exif", "")
    if allow_mixed:
        lossless = False

    # Sensible keyframe defaults are from gif2webp.c script
    if kmin is None:
        kmin = 9 if lossless else 3
    if kmax is None:
        kmax = 17 if lossless else 5

    # Validate background color
    if (not isinstance(background, (list, tuple)) or len(background) != 4 or
            not all(v >= 0 and v < 256 for v in background)):
        raise IOError("Background color is not an RGBA tuple clamped to (0-255): %s" % str(background))
    bg_r, bg_g, bg_b, bg_a = background
    background = (bg_a << 24) | (bg_r << 16) | (bg_g << 8) | (bg_b << 0) # Convert to packed uint

    # Setup the WebP animation encoder
    enc = _webp.WebPAnimEncoder(
        im.size[0], im.size[1],
        background,
        loop,
        minimize_size,
        kmin, kmax,
        allow_mixed,
        verbose
    )

    # Add each frame
    frame_idx = 0
    timestamp = 0
    cur_idx = im.tell()
    try:
        for ims in [im]+append_images:
            # Get # of frames in this image
            if not hasattr(ims, "n_frames"):
                nfr = 1
            else:
                nfr = ims.n_frames

            for idx in range(nfr):
                ims.seek(idx)
                ims.load()

                # Make sure image mode is supported
                frame = ims
                if not ims.mode in _VALID_WEBP_MODES:
                    alpha = 'A' in ims.im.getpalettemode()
                    frame = image.convert('RGBA' if alpha else 'RGB')

                # Append the frame to the animation encoder
                enc.add(
                    frame.tobytes(),
                    timestamp,
                    frame.size[0], frame.size[1],
                    frame.mode,
                    lossless,
                    quality,
                    method
                )

                # Update timestamp and frame index
                timestamp += duration[frame_idx] if isinstance(duration, (list, tuple)) else duration
                frame_idx += 1

    finally:
        im.seek(cur_idx)

    # Force encoder to flush frames
    enc.add(
        None,
        timestamp,
        0, 0, "", lossless, quality, 0
    )

    # Get the final output from the encoder
    data = enc.assemble(icc_profile, exif)
    if data is None:
        raise IOError("cannot write file as WEBP (encoder returned None)")

    fp.write(data)

def _save(im, fp, filename):
    if _webp.HAVE_WEBPMUX:
        _save_all(im, fp, filename)
        return

    image_mode = im.mode
    if im.mode not in _VALID_WEBP_MODES:
        alpha = 'A' in im.im.getpalettemode()
        im = im.convert('RGBA' if alpha else 'RGB')

    lossless = im.encoderinfo.get("lossless", False)
    quality = im.encoderinfo.get("quality", 80)
    icc_profile = im.encoderinfo.get("icc_profile", "")
    exif = im.encoderinfo.get("exif", "")

    data = _webp.WebPEncode(
        im.tobytes(),
        im.size[0],
        im.size[1],
        lossless,
        float(quality),
        im.mode,
        icc_profile,
        exif
    )
    if data is None:
        raise IOError("cannot write file as WEBP (encoder returned None)")

    fp.write(data)


Image.register_open(WebPImageFile.format, WebPImageFile, _accept)
Image.register_save(WebPImageFile.format, _save)
Image.register_save_all(WebPImageFile.format, _save_all)
Image.register_extension(WebPImageFile.format, ".webp")
Image.register_mime(WebPImageFile.format, "image/webp")
