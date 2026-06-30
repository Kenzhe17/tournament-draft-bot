import cv2
import numpy as np
import re
from typing import Optional
from paddleocr import PaddleOCR
from .preprocess import ImagePreprocessor
from .models import OCRResult


class NicknameReader:
    """Read player nicknames using OCR."""
    
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
    
    def read(self, image: np.ndarray) -> OCRResult:
        """
        Read nickname from cropped image.
        
        Args:
            image: Cropped nickname region image
            
        Returns:
            OCRResult with text and confidence
        """
        if image.size == 0:
            return OCRResult("", 0.0)
        
        # Preprocess for OCR
        processed = ImagePreprocessor.preprocess_for_ocr(image)
        
        # Resize for better accuracy
        processed = ImagePreprocessor.resize_for_ocr(processed, scale=2.0)
        
        # Run OCR
        ocr_result = self.ocr.ocr(processed, cls=True)
        
        if not ocr_result or not ocr_result[0]:
            return OCRResult("", 0.0)
        
        # Get the best result
        best_line = ocr_result[0][0]
        text = best_line[1][0].strip()
        confidence = best_line[1][1]
        
        # Clean the text
        text = self._clean_nickname(text)
        
        return OCRResult(text, confidence)
    
    def _clean_nickname(self, text: str) -> str:
        """
        Clean and normalize nickname text.
        
        Args:
            text: Raw OCR text
            
        Returns:
            Cleaned nickname
        """
        # Remove common OCR artifacts
        text = re.sub(r'[|l]', 'I', text)  # Common confusion
        text = re.sub(r'[O0]', 'O', text)  # Common confusion
        text = re.sub(r'\s+', '', text)  # Remove spaces within nickname
        
        # Keep special characters that are valid in nicknames
        # Remove only obvious garbage
        text = re.sub(r'[^\w\[\]\{\}\(\)\-\_\+\=\@\#\$\%\^\&\*\!\~\`\'\"\;\\\:\,\.\<\>\?\/\|♡♥★☆]', '', text)
        
        return text.strip()
