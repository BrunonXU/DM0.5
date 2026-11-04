"""Transport exact model pixels using the bundle's frozen preprocessing source."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path

from PIL import Image

_CAMERAS = ('head', 'left_wrist', 'right_wrist')
_MASKED_LEFT_PNG_SHA256 = 'd61b446bab0457ddc5db93d58bcffad17606477b9b4e311095d99671e82d1f46'


class PixelEncoder:
    def __init__(self, directory, identity, *, workers=1, masked_left_transport=False):
        if type(workers) is not int or not 1 <= workers <= 3:
            raise ValueError('RTC pixel workers must be an integer from 1 to 3')
        if type(masked_left_transport) is not bool:
            raise ValueError('RTC masked left transport must be a boolean')
        self.workers = workers
        self.executor = None
        self.masked_left_transport = masked_left_transport
        self._masked_left_png = None
        directory=Path(directory)
        source=directory/'processing.py'
        contract=directory/'contract.json'
        for path,key in ((source,'implementation_sha256'),(contract,'contract_sha256')):
            if hashlib.sha256(path.read_bytes()).hexdigest()!=identity[key]:
                raise ValueError('RTC pixel preprocessing identity mismatch')
        spec=importlib.util.spec_from_file_location('f1_rtc_frozen_processing',source)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.contract=json.loads(contract.read_text())
        self.identity=dict(identity)
        if self.masked_left_transport:
            self._validate_masked_contract()
            out = BytesIO()
            Image.new('RGB', (224, 224), (0, 0, 0)).save(out, format='PNG', compress_level=1)
            self._masked_left_png = out.getvalue()
            if (len(self._masked_left_png) != 738
                    or hashlib.sha256(self._masked_left_png).hexdigest() != _MASKED_LEFT_PNG_SHA256):
                raise ValueError('RTC masked left PNG differs from the validated transport asset')

    def _validate_masked_contract(self):
        expected = dict(schema='f1_toys_eef_contract_v2', contract='f1_upper_16d_v1',
                        input_version='right_only_v1', cameras=list(_CAMERAS),
                        active_cameras=['head', 'right_wrist'], image_size=224,
                        active_state_indices=[7,8,9,10,11,12,13,15], state_dim=16)
        if any(self.contract.get(key) != value for key, value in expected.items()):
            raise ValueError('RTC masked left transport requires the exact right_only_v1 input contract')

    @property
    def transport_identity(self):
        if not self.masked_left_transport:
            return dict(mode='png1_rgb224_v1', constant_cameras=[])
        return dict(mode='masked_left_png_v1', constant_cameras=['left_wrist'],
                    png_sha256=_MASKED_LEFT_PNG_SHA256,
                    contract_sha256=self.identity['contract_sha256'])

    def encode(self, frames):
        # Only process this request's frames: no background capture or stale cache.
        frames = tuple(frames)
        if self.masked_left_transport:
            self._validate_masked_contract()
            if tuple(frame.camera for frame in frames) != _CAMERAS:
                raise ValueError('RTC masked transport requires all three ordered source frames')
        if self.workers == 1:
            return tuple(self._encode_frame(frame) for frame in frames)
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='rtc-pixels')
        futures = [None if self.masked_left_transport and frame.camera == 'left_wrist'
                   else self.executor.submit(self._encode_frame, frame) for frame in frames]
        return tuple(self._encode_frame(frame) if future is None else future.result()
                     for frame, future in zip(frames, futures))

    def _encode_frame(self, frame):
        if self.masked_left_transport and frame.camera == 'left_wrist':
            # Only the transport copy is synthetic. Preserve the raw source
            # frame, its identity/timestamps, and all recording ownership.
            return replace(frame, payload=self._masked_left_png,
                           media_type='image/png', width=224, height=224)
        with Image.open(BytesIO(frame.payload)) as image:
            pixels=self.module.process_image(image,frame.camera,self.contract)
        out=BytesIO()
        Image.fromarray(pixels).save(out,format='PNG',compress_level=1)
        return replace(frame,payload=out.getvalue(),media_type='image/png',width=224,height=224)

    def close(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True)
            self.executor = None
