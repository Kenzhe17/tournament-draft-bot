import cv2
import numpy as np
from typing import List
from .models import AbsoluteBoundingBox


class RowDetector:
    """Detect individual player rows in team sections."""
    
    def __init__(self):
        self.expected_rows = 4
    
    def detect(self, team_image: np.ndarray) -> List[AbsoluteBoundingBox]:
        """
        Detect player rows in a team section.
        
        Args:
            team_image: Team section image
            
        Returns:
            List of row bounding boxes
        """
        height, width = team_image.shape[:2]
        
        # Try multiple detection methods
        methods = [
            self._detect_by_horizontal_projection,
            self._detect_by_contours,
            self._detect_by_equal_division
        ]
        
        for method in methods:
            rows = method(team_image)
            if len(rows) == self.expected_rows:
                return rows
        
        # Fallback to equal division
        return self._detect_by_equal_division(team_image)
    
    def _detect_by_horizontal_projection(self, image: np.ndarray) -> List[AbsoluteBoundingBox]:
        """Detect rows using horizontal projection profile."""
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        
        # Binarize
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Horizontal projection
        horizontal_proj = np.sum(binary, axis=1)
        
        # Smooth the projection
        kernel_size = 5
        kernel = np.ones(kernel_size) / kernel_size
        horizontal_proj = np.convolve(horizontal_proj, kernel, mode='same')
        
        # Find peaks
        threshold = np.max(horizontal_proj) * 0.2
        row_indices = np.where(horizontal_proj > threshold)[0]
        
        if len(row_indices) == 0:
            return []
        
        # Group consecutive indices into rows
        rows = []
        current_row = [row_indices[0]]
        
        for idx in row_indices[1:]:
            if idx - current_row[-1] <= 10:  # Within 10 pixels
                current_row.append(idx)
            else:
                if current_row:
                    y_min = min(current_row)
                    y_max = max(current_row)
                    rows.append(AbsoluteBoundingBox(0, y_min, image.shape[1], y_max - y_min + 1))
                current_row = [idx]
        
        if current_row:
            y_min = min(current_row)
            y_max = max(current_row)
            rows.append(AbsoluteBoundingBox(0, y_min, image.shape[1], y_max - y_min + 1))
        
        # Filter and sort
        rows = [r for r in rows if r.height > image.shape[0] * 0.05]
        rows.sort(key=lambda r: r.y)
        
        return rows
    
    def _detect_by_contours(self, image: np.ndarray) -> List[AbsoluteBoundingBox]:
        """Detect rows using contour analysis."""
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        
        # Binarize
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Find contours
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        height, width = image.shape[:2]
        min_area = width * height * 0.02
        
        rows = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            
            if area > min_area and h > height * 0.05:
                rows.append(AbsoluteBoundingBox(x, y, w, h))
        
        # Sort by y position
        rows.sort(key=lambda r: r.y)
        
        return rows
    
    def _detect_by_equal_division(self, image: np.ndarray) -> List[AbsoluteBoundingBox]:
        """Fallback: divide image into equal rows."""
        height, width = image.shape[:2]
        
        row_height = height // self.expected_rows
        rows = []
        
        for i in range(self.expected_rows):
            y = i * row_height
            # Add small padding
            padding = 2
            y = max(0, y - padding)
            h = min(row_height + padding * 2, height - y)
            rows.append(AbsoluteBoundingBox(0, y, width, h))
        
        return rows
