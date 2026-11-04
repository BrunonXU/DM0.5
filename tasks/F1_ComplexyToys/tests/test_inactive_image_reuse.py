"""Guard against stale or active visual features entering the captured prefix."""
from types import SimpleNamespace
import unittest
import torch
from f1_toys.inactive_image_reuse import InactiveImageReuse


class Vision(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.scale = torch.nn.Parameter(torch.tensor(2.))
        self.calls = 0
    def forward(self, image):
        self.calls += 1
        return image * self.scale


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.model = SimpleNamespace(training=False, img=Vision())
        self.cache = InactiveImageReuse(self.model)
        self.obs = SimpleNamespace(images={'left':torch.zeros(2),'head':torch.ones(2)},
                                   image_masks={'left':torch.tensor([False]),'head':torch.tensor([True])})

    @torch.no_grad()
    def test_active_view_recomputed_and_inactive_reused_exactly(self):
        self.assertTrue(self.cache.prepare(self.obs))
        before=self.model.img.calls
        with self.cache.apply(self.obs):
            left=self.model.img(self.obs.images['left'])
            head=self.model.img(self.obs.images['head'])
        self.assertEqual(self.model.img.calls,before+1)
        torch.testing.assert_close(left,torch.zeros(2),rtol=0,atol=0)
        torch.testing.assert_close(head,torch.full((2,),2.),rtol=0,atol=0)
        self.assertNotIn('forward',vars(self.model.img))
        self.assertFalse(self.cache.prepare(self.obs))

    @torch.no_grad()
    def test_changed_pixels_invalidate(self):
        self.cache.prepare(self.obs); self.obs.images['left'].add_(1)
        self.assertTrue(self.cache.prepare(self.obs))
        with self.cache.apply(self.obs):
            torch.testing.assert_close(self.model.img(self.obs.images['left']),torch.full((2,),2.),rtol=0,atol=0)

    @torch.no_grad()
    def test_mask_becomes_active_removes_cached_slot(self):
        self.cache.prepare(self.obs);self.obs.image_masks['left'].fill_(True)
        self.assertTrue(self.cache.prepare(self.obs));self.assertFalse(self.cache.entries)

    @torch.no_grad()
    def test_active_tensor_alias_is_never_reused(self):
        self.obs.images['left']=self.obs.images['head']
        self.cache.prepare(self.obs);self.assertFalse(self.cache.entries)
        before=self.model.img.calls
        with self.cache.apply(self.obs):self.model.img(self.obs.images['head'])
        self.assertEqual(self.model.img.calls,before+1)

    @torch.no_grad()
    def test_changed_dtype_invalidates(self):
        self.cache.prepare(self.obs);self.obs.images['left']=self.obs.images['left'].double()
        self.assertTrue(self.cache.prepare(self.obs))
        self.assertEqual(self.cache.entries['left']['output'].dtype,torch.float64)

    @torch.no_grad()
    def test_changed_weights_rejected(self):
        self.cache.prepare(self.obs);self.model.img.scale.add_(1)
        with self.assertRaisesRegex(RuntimeError,'weights changed'):self.cache.prepare(self.obs)

    def test_gradient_and_training_paths_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'no-grad'):self.cache.prepare(self.obs)
        self.model.training=True
        with torch.no_grad(),self.assertRaisesRegex(RuntimeError,'frozen'):self.cache.prepare(self.obs)

    @torch.no_grad()
    def test_exception_restores_existing_override(self):
        forward=self.model.img.forward;self.model.img.forward=forward
        self.cache.prepare(self.obs)
        with self.assertRaisesRegex(ValueError,'test'):
            with self.cache.apply(self.obs):raise ValueError('test')
        self.assertIs(self.model.img.forward,forward)

    @torch.no_grad()
    def test_new_instance_never_inherits_cache(self):
        self.cache.prepare(self.obs)
        other=InactiveImageReuse(SimpleNamespace(training=False,img=Vision()))
        self.assertFalse(other.entries);self.assertTrue(other.prepare(self.obs))

    @torch.no_grad()
    def test_optional_vision_outputs_are_preserved(self):
        original=self.model.img.forward
        self.model.img.forward=lambda image: (original(image), None, {'optional':None})
        self.cache.prepare(self.obs)
        with self.cache.apply(self.obs):
            output=self.model.img(self.obs.images['left'])
        self.assertIsNone(output[1]);self.assertEqual(output[2],{'optional':None})
        torch.testing.assert_close(output[0],torch.zeros(2),rtol=0,atol=0)


if __name__=='__main__':unittest.main()
