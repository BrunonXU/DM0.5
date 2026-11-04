"""DM recipe operations, with independent per-draw/camera/operation RNG streams."""
import hashlib

import albumentations as A


def augment(pixels, camera, seed):
    steps = []
    if camera == "head":
        steps += [A.RandomResizedCrop(size=(224, 224), scale=(.95, .95), ratio=(1., 1.), p=.5),
                  A.Rotate(limit=(-2, 2), p=.5)]
    steps += [A.ColorJitter(brightness=.3, contrast=.4, saturation=.3, hue=0., p=.5)]
    for step in steps:
        key = f"f1-toys-recipe-preview:{seed}:{camera}:{type(step).__name__}".encode()
        step.set_random_seed(int.from_bytes(hashlib.sha256(key).digest()[:4], "little"))
        pixels = step(image=pixels)["image"]
    return pixels
