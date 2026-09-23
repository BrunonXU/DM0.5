"""F1 task-side data adapter; does not import or launch robot control."""

from .reader import Episode, F1Dataset, Normalizer, fit_statistics

__all__ = ["Episode", "F1Dataset", "Normalizer", "fit_statistics"]
