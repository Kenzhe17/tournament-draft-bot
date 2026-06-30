import cv2
import numpy as np
import re
from typing import Tuple
from paddleocr import PaddleOCR
from .preprocess import ImagePreprocessor
from .models import OCRResult


class DamageReader:
    """Read damage values using OCR."""
    
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
    
    def read(self, image: np.ndarray) -> Tuple[int, float]:
        """
        Read damage from cropped image.
        
        Args:
            image: Cropped damage region image
            
        Returns:
            Tuple of (damage, confidence)
        """
        if image.size == 0:
            return 0, 0.0
        
        # Preprocess for OCR
        processed = ImagePreprocessor.preprocess_for_ocr(image)
        
        # Resize for better accuracy
        processed = ImagePreprocessor.resize_for_ocr(processed, scale=2.0)
        
        # Run OCR
        ocr_result = self.ocr.ocr(processed, cls=True)
        
        if not ocr_result or not ocr_result[0]:
            return 0, 0.0
        
        # Get the best result
        best_line = ocr_result[0][0]
        text = best_line[1][0].strip()
        confidence = best_line[1][1]
        
        # Parse damage
        damage = self._parse_damage(text)
        
        return damage, confidence
    
    def _parse_damage(self, text: str) -> int:
        """
        Parse damage string into integer.
        
        Args:
            text: OCR text (e.g., "4101")
            
        Returns:
            Damage value as integer
        """
        # Extract numbers
        numbers = re.findall(r'\d+', text)
        
        if numbers:
            # Take the largest number (likely the damage)
            return max(int(num) for num in numbers)
        
        return 0
