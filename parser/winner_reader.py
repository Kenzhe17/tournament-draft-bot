import cv2
import numpy as np
from typing import Optional
from paddleocr import PaddleOCR
from .preprocess import ImagePreprocessor


class WinnerReader:
    """Determine the winning team from the match result."""
    
    WINNER_KEYWORDS_TEAM1 = ['VICTORY', 'ПОБЕДА', 'WIN', 'Победа', 'Victory']
    WINNER_KEYWORDS_TEAM2 = ['DEFEAT', 'ПОРАЖЕНИЕ', 'LOSE', 'Поражение', 'Defeat']
    
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
    
    def read(self, image: np.ndarray) -> Optional[int]:
        """
        Determine winning team from image.
        
        Args:
            image: Full match image or score region
            
        Returns:
            Team ID (1 or 2) or None if cannot determine
        """
        if image.size == 0:
            return None
        
        # Focus on top region where victory/defeat is usually shown
        height, width = image.shape[:2]
        top_region = image[:int(height * 0.2), :]
        
        # Preprocess
        processed = ImagePreprocessor.preprocess_for_ocr(top_region)
        
        # Run OCR
        ocr_result = self.ocr.ocr(processed, cls=True)
        
        if not ocr_result or not ocr_result[0]:
            return None
        
        # Check for winner keywords
        for line in ocr_result[0]:
            text = line[1][0].strip()
            
            for keyword in self.WINNER_KEYWORDS_TEAM1:
                if keyword.lower() in text.lower():
                    return 1
            
            for keyword in self.WINNER_KEYWORDS_TEAM2:
                if keyword.lower() in text.lower():
                    return 2
        
        return None
    
    def read_from_score(self, score: tuple) -> int:
        """
        Determine winner from score values.
        
        Args:
            score: Tuple of (team1_score, team2_score)
            
        Returns:
            Team ID (1 or 2)
        """
        team1_score, team2_score = score
        
        if team1_score > team2_score:
            return 1
        elif team2_score > team1_score:
            return 2
        
        return 0  # Draw
