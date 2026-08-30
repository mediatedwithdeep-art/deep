"""Sentinel AI pipeline.

Video -> frame sampling -> detection -> tracking -> quality gate ->
ANPR + ReID + attributes -> tracklet close-out -> Sighting.

Two backends behind one interface:
  simulation : no model weights, no GPU. Converts simulator ground truth
               into realistically noisy observations. This is what makes
               the demo runnable on a laptop -- and, because ground truth
               is known, it is also what lets us measure real precision
               and recall instead of asserting numbers.
  onnx       : real models via onnxruntime, CPU or CUDA.

Switching is one setting (AI_BACKEND). Nothing downstream of `pipeline.py`
knows or cares which is running.
"""
__version__ = "1.0.0"
