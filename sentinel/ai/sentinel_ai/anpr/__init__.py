"""ANPR: plate detection, OCR, normalisation, confidence."""
from .ocr import PlateRecognizer, SimulatedPlateRecognizer, create_recognizer
__all__ = ["PlateRecognizer", "SimulatedPlateRecognizer", "create_recognizer"]
