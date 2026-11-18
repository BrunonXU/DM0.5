"""Decode color to RGB and depth to native units, respecting stride and endianness."""
import cv2
import numpy as np
from collections import OrderedDict
from threading import RLock


def decode(frame, data: bytes):
    encoding = frame.encoding.lower()
    if "jpeg" in encoding or "png" in encoding:
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None or image.shape[:2] != (frame.height, frame.width):
            raise ValueError("compressed_image_dimensions_mismatch")
        if frame.stream == "rgb" and image.ndim == 3:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    else:
        formats = {"rgb8": (np.uint8,3), "bgr8": (np.uint8,3), "mono8": (np.uint8,1),
                   "16uc1": (np.uint16,1), "32fc1": (np.float32,1)}
        if encoding not in formats:
            raise ValueError("unsupported_image_encoding")
        dtype, channels = formats[encoding]
        dtype = np.dtype(dtype).newbyteorder(">" if frame.is_bigendian else "<")
        width_bytes = frame.width * channels * dtype.itemsize
        step = frame.row_step_bytes or width_bytes
        if step < width_bytes or len(data) != step * frame.height:
            raise ValueError("image_stride_or_length_mismatch")
        raw = np.frombuffer(data,np.uint8).reshape(frame.height,step)[:,:width_bytes].copy()
        image = raw.view(dtype).reshape(frame.height,frame.width,channels)
        if channels == 1:
            image = image[:,:,0]
        elif encoding == "bgr8":
            image = cv2.cvtColor(image,cv2.COLOR_BGR2RGB)
    if frame.stream == "depth":
        if image.ndim != 2 or frame.depth_scale_m is None:
            raise ValueError("depth_units_or_dimensions_missing")
        if image.dtype.kind == "f" and frame.depth_scale_m != 1.:
            raise ValueError("float_depth_must_be_explicit_meters")
        if image.dtype.kind == "u" and frame.depth_scale_m == 1.:
            raise ValueError("integer_depth_meter_scale_unverified")
    return image


class DecodedFrameCache:
    """Bounded, immutable pixels for exactly the same frame and encoded bytes.

    Frame identity includes acquisition clocks, sequence and calibration. This
    cache does not validate freshness or turn repeated pixels into new evidence.
    """
    def __init__(self, max_bytes=64 * 1024**2, max_entries=24):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def decode(self, frame, data):
        data = bytes(data)
        key = (frame, data)
        with self._lock:
            cached = self._items.get(key)
            if cached is not None:
                self._items.move_to_end(key)
                return cached[0]
        # Decode outside the lock so independent camera streams remain parallel.
        image = decode(frame, data)
        # Immutable backing also prevents consumers from re-enabling writes.
        image = np.frombuffer(image.tobytes(), dtype=image.dtype).reshape(image.shape)
        size = len(data) + image.nbytes
        if size > self.max_bytes or self.max_entries < 1:
            return image
        with self._lock:
            cached = self._items.get(key)
            if cached is not None:
                self._items.move_to_end(key)
                return cached[0]
            while self._items and (self._bytes + size > self.max_bytes or len(self._items) >= self.max_entries):
                _, (_, removed) = self._items.popitem(last=False)
                self._bytes -= removed
            self._items[key] = (image, size)
            self._bytes += size
        return image


_decoded_frames = DecodedFrameCache()


def decode_cached(frame, data):
    # Raw RGB/depth decoding is cheaper than immutable-copy and key comparison.
    # Retain the existing raw path; cache only costly compressed decoding.
    if not any(codec in frame.encoding.lower() for codec in ('jpeg', 'png')):
        return decode(frame, data)
    return _decoded_frames.decode(frame, data)
