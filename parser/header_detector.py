import cv2
import numpy as np
from typing import Optional, Dict
from paddleocr import PaddleOCR
from .models import AbsoluteBoundingBox


class HeaderDetector:
    """Detect table headers (Имя, K/D/A, DMG) using OCR and CV."""
    
    HEADER_KEYWORDS = {
        'name': ['Имя', 'Name', 'Nombre', 'Nome', 'NAME'],
        'kda': ['K/D/A', 'KDA', 'K/D', 'K/D/A'],
        'dmg': ['DMG', 'Damage', 'DAMAGE', 'УРОН']
    }
    
    def __init__(self):
        self.ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
    
    def detect(self, image: np.ndarray) -> Dict[str, AbsoluteBoundingBox]:
        """
        Detect header positions.
        
        Args:
            image: Table image
            
        Returns:
            Dictionary mapping header names to their bounding boxes
        """
        result = {}
        
        # Run OCR on the image
        ocr_result = self.ocr.ocr(image, cls=True)
        
        if not ocr_result or not ocr_result[0]:
            # Fallback to heuristic positioning
            return self._detect_by_heuristics(image)
        
        # Process OCR results
        for line in ocr_result[0]:
            text = line[1][0].strip()
            bbox = line[0]
            
            # Convert bbox to AbsoluteBoundingBox
            x = int(min(point[0] for point in bbox))
            y = int(min(point[1] for point in bbox))
            width = int(max(point[0] for point in bbox) - x)
            height = int(max(point[1] for point in bbox) - y)
            
            # Match against keywords
            for header_type, keywords in self.HEADER_KEYWORDS.items():
                if any(keyword.lower() in text.lower() for keyword in keywords):
                    result[header_type] = AbsoluteBoundingBox(x, y, width, height)
                    break
        
        # If any header is missing, use heuristics
        if len(result) < 3:
            return self._detect_by_heuristics(image)
        
        return result
    
    def _detect_by_heuristics(self, image: np.ndarray) -> Dict[str, AbsoluteBoundingBox]:
        """
        Fallback: detect headers using positional heuristics.
        
        Args:
            image: Table image
            
        Returns:
            Dictionary mapping header names to their bounding boxes
        """
        height, width = image.shape[:2]
        
        # Typical header row is at the top of the table
        header_y = int(height * 0.05)
        header_height = int(height * 0.1)
        
        # Typical column positions (relative to width)
        name_x = int(width * 0.05)
        name_width = int(width * 0.35)
        
        kda_x = int(width * 0.45)
        kda_width = int(width * 0.2)
        
        dmg_x = int(width * 0.7)
        dmg_width = int(width * 0.25)
        
        return {
            'name': AbsoluteBoundingBox(name_x, header_y, name_width, header_height),
            'kda': AbsoluteBoundingBox(kda_x, header_y, kda_width, header_height),
            'dmg': AbsoluteBoundingBox(dmg_x, header_y, dmg_width, header_height)
        }
    
    def get_column_regions(self, headers: Dict[str, AbsoluteBoundingBox], 
                           row_bbox: AbsoluteBoundingBox) -> Dict[str, AbsoluteBoundingBox]:
        """
        Get column regions for a specific row based on header positions.
        
        Args:
            headers: Header bounding boxes
            row_bbox: Row bounding box
            
        Returns:
            Dictionary mapping column names to their bounding boxes in the row
        """
        regions = {}
        
        row_y = row_bbox.y
        row_height = row_bbox.height
        
        for header_name, header_bbox in headers.items():
            regions[header_name] = AbsoluteBoundingBox(
                header_bbox.x,
                row_y,
                header_bbox.width,
                row_height
            )
        
        return regions
