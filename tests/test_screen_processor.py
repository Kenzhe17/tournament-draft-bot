"""Unit tests for screen_processor.py."""

import unittest
import numpy as np
import cv2
from pathlib import Path
import sys

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.screen_processor import ScreenProcessor


class TestScreenProcessor(unittest.TestCase):
    """Test cases for ScreenProcessor class."""

    def setUp(self):
        """Set up test fixtures."""
        self.processor = ScreenProcessor()

    def test_crop_roi(self):
        """Test ROI cropping functionality."""
        # Create a simple test image (100x100)
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[10:30, 10:30] = [255, 255, 255]  # White square

        # Crop the ROI
        roi = self.processor.crop_roi(image, (10, 10, 20, 20))

        # Check that the cropped image has the correct size
        self.assertEqual(roi.shape, (20, 20, 3))

        # Check that the cropped area contains the white square
        self.assertTrue(np.all(roi == 255))

    def test_preprocess_for_ocr(self):
        """Test OCR preprocessing."""
        # Create a simple test image with some text-like pattern
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        image[20:80, 20:80] = [128, 128, 128]  # Gray area

        # Preprocess
        preprocessed = self.processor.preprocess_for_ocr(image, is_number=False)

        # Check that the result is grayscale
        self.assertEqual(len(preprocessed.shape), 2)

        # Check that the result is binary (only 0 or 255)
        unique_values = np.unique(preprocessed)
        self.assertTrue(all(v in [0, 255] for v in unique_values))

    def test_extract_number(self):
        """Test number extraction from image."""
        # Create a simple test image with a number-like pattern
        # This is a basic test - real OCR accuracy depends on Tesseract
        image = np.zeros((50, 50, 3), dtype=np.uint8)
        image[10:40, 10:40] = [255, 255, 255]  # White area

        # Extract number (will likely return 0 for this simple pattern)
        number = self.processor.extract_number(image)

        # Should return an integer
        self.assertIsInstance(number, int)

    def test_find_scale_factor_without_anchor(self):
        """Test scale factor calculation without anchor image."""
        # Create a test image at different resolution
        image = np.zeros((720, 1280, 3), dtype=np.uint8)

        # Without anchor image, should calculate based on width
        scale_factor = self.processor.find_scale_factor(image)

        # Should return a positive float
        self.assertIsInstance(scale_factor, float)
        self.assertGreater(scale_factor, 0)

    def test_normalize_image(self):
        """Test image normalization to 1920x1080."""
        # Create a test image at half resolution
        image = np.zeros((540, 960, 3), dtype=np.uint8)

        # Normalize with scale factor 2.0
        normalized = self.processor.normalize_image(image, 2.0)

        # Check that the result is 1920x1080
        self.assertEqual(normalized.shape[1], 1920)
        self.assertEqual(normalized.shape[0], 1080)

    def test_normalize_image_crop(self):
        """Test image normalization with cropping."""
        # Create a test image larger than reference
        image = np.zeros((1200, 2000, 3), dtype=np.uint8)

        # Normalize with scale factor 1.0 (should crop)
        normalized = self.processor.normalize_image(image, 1.0)

        # Check that the result is 1920x1080
        self.assertEqual(normalized.shape[1], 1920)
        self.assertEqual(normalized.shape[0], 1080)

    def test_normalize_image_pad(self):
        """Test image normalization with padding."""
        # Create a test image smaller than reference
        image = np.zeros((500, 800, 3), dtype=np.uint8)

        # Normalize with scale factor 2.4 (should pad)
        normalized = self.processor.normalize_image(image, 2.4)

        # Check that the result is 1920x1080
        self.assertEqual(normalized.shape[1], 1920)
        self.assertEqual(normalized.shape[0], 1080)

    def test_process_screenshot_invalid_path(self):
        """Test processing with invalid image path."""
        with self.assertRaises(ValueError):
            self.processor.process_screenshot("nonexistent_image.png")

    def test_process_screenshot_from_bytes_invalid(self):
        """Test processing with invalid bytes."""
        with self.assertRaises(ValueError):
            self.processor.process_screenshot_from_bytes(b"invalid image data")


if __name__ == "__main__":
    unittest.main()
