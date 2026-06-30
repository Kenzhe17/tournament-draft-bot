import cv2
import numpy as np
import re
from typing import Tuple, Optional
from paddleocr import PaddleOCR
from .preprocess import ImagePreprocessor
from .models import OCRResult


class KDAReader:
    """Read K/D/A (Kills/Deaths/Assists) using OCR."""
    
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
    
    def read(self, image: np.ndarray) -> Tuple[int, int, int, float]:
        """
        Read K/D/A from cropped image.
        
        Args:
            image: Cropped K/D/A region image
            
        Returns:
            Tuple of (kills, deaths, assists, confidence)
        """
        if image.size == 0:
            return 0, 0, 0, 0.0
        
        # Preprocess for OCR
        processed = ImagePreprocessor.preprocess_for_ocr(image)
        
        # Resize for better accuracy
        processed = ImagePreprocessor.resize_for_ocr(processed, scale=2.0)
        
        # Run OCR
        ocr_result = self.ocr.ocr(processed, cls=True)
        
        if not ocr_result or not ocr_result[0]:
            return 0, 0, 0, 0.0
        
        # Get the best result
        best_line = ocr_result[0][0]
        text = best_line[1][0].strip()
        confidence = best_line[1][1]
        
        # Parse K/D/A
        kills, deaths, assists = self._parse_kda(text)
        
        return kills, deaths, assists, confidence
    
    def _parse_kda(self, text: str) -> Tuple[int, int, int]:
        """
        Parse K/D/A string into individual values.
        
        Args:
            text: OCR text (e.g., "11/8/2")
            
        Returns:
            Tuple of (kills, deaths, assists)
        """
        # Try to match pattern like "11/8/2"
        match = re.search(r'(\d+)\s*/\s*(\d+)\s*/\s*(\d+)', text)
        
        if match:
            kills = int(match.group(1))
            deaths = int(match.group(2))
            assists = int(match.group(3))
            return kills, deaths, assists
        
        # Try alternative separators
        for sep in ['-', '\\', ':']:
            pattern = f'(\\d+)\\s*{re.escape(sep)}\\s*(\\d+)\\s*{re.escape(sep)}\\s*(\\d+)'
            match = re.search(pattern, text)
            if match:
                kills = int(match.group(1))
                deaths = int(match.group(2))
                assists = int(match.group(3))
                return kills, deaths, assists
        
        # Fallback: try to extract any three numbers
        numbers = re.findall(r'\d+', text)
        if len(numbers) >= 3:
            return int(numbers[0]), int(numbers[1]), int(numbers[2])
        
        return 0, 0, 0
