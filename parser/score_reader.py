import cv2
import numpy as np
import re
from typing import Optional, Tuple
from paddleocr import PaddleOCR
from .preprocess import ImagePreprocessor
from .models import Score


class ScoreReader:
    """Read match score (e.g., 7:6) using OCR."""
    
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
    
    def read(self, image: np.ndarray) -> Optional[Score]:
        """
        Read score from cropped image.
        
        Args:
            image: Cropped score region image
            
        Returns:
            Score object or None
        """
        if image.size == 0:
            return None
        
        # Preprocess for OCR
        processed = ImagePreprocessor.preprocess_for_ocr(image)
        
        # Resize for better accuracy
        processed = ImagePreprocessor.resize_for_ocr(processed, scale=2.0)
        
        # Run OCR
        ocr_result = self.ocr.ocr(processed, cls=True)
        
        if not ocr_result or not ocr_result[0]:
            return None
        
        # Get the best result
        best_line = ocr_result[0][0]
        text = best_line[1][0].strip()
        
        # Parse score
        score = self._parse_score(text)
        
        return score
    
    def _parse_score(self, text: str) -> Optional[Score]:
        """
        Parse score string into Score object.
        
        Args:
            text: OCR text (e.g., "7:6", "7 - 6")
            
        Returns:
            Score object or None
        """
        # Try pattern like "7:6"
        match = re.search(r'(\d+)\s*[:\-\s]\s*(\d+)', text)
        
        if match:
            team1_score = int(match.group(1))
            team2_score = int(match.group(2))
            return Score(team1_score, team2_score)
        
        # Try to extract any two numbers
        numbers = re.findall(r'\d+', text)
        if len(numbers) >= 2:
            return Score(int(numbers[0]), int(numbers[1]))
        
        return None
    
    def find_score_region(self, image: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        """
        Find the score region in the full image.
        
        Args:
            image: Full match image
            
        Returns:
            Tuple of (x, y, width, height) or None
        """
        height, width = image.shape[:2]
        
        # Score is typically in the top center
        score_x = int(width * 0.3)
        score_y = int(height * 0.1)
        score_w = int(width * 0.4)
        score_h = int(height * 0.1)
        
        return (score_x, score_y, score_w, score_h)
