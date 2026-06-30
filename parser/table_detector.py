import cv2
import numpy as np
from typing import Tuple, Optional, List
from .models import BoundingBox, AbsoluteBoundingBox


class TableDetector:
    """Detect the player statistics table in Free Fire screenshots."""
    
    def __init__(self):
        self.min_table_height_ratio = 0.3
        self.min_table_width_ratio = 0.5
    
    def detect(self, image: np.ndarray) -> Optional[AbsoluteBoundingBox]:
        """
        Detect the main statistics table.
        
        Args:
            image: Input image
            
        Returns:
            Absolute bounding box of the table or None
        """
        height, width = image.shape[:2]
        
        # Try multiple detection methods
        methods = [
            self._detect_by_lines,
            self._detect_by_text_regions,
            self._detect_by_heuristics
        ]
        
        for method in methods:
            bbox = method(image)
            if bbox and self._validate_bbox(bbox, width, height):
                return bbox
        
        return None
    
    def _detect_by_lines(self, image: np.ndarray) -> Optional[AbsoluteBoundingBox]:
        """Detect table using horizontal and vertical lines."""
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        
        # Detect horizontal lines
        horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
        horizontal = cv2.morphologyEx(gray, cv2.MORPH_OPEN, horizontal_kernel)
        
        # Detect vertical lines
        vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 40))
        vertical = cv2.morphologyEx(gray, cv2.MORPH_OPEN, vertical_kernel)
        
        # Combine
        lines = cv2.add(horizontal, vertical)
        
        # Find contours
        contours, _ = cv2.findContours(lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        height, width = image.shape[:2]
        min_area = width * height * 0.1
        
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            
            if area > min_area and h > height * 0.2 and w > width * 0.3:
                return AbsoluteBoundingBox(x, y, w, h)
        
        return None
    
    def _detect_by_text_regions(self, image: np.ndarray) -> Optional[AbsoluteBoundingBox]:
        """Detect table by finding dense text regions."""
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        
        # Use MSER to detect text regions
        mser = cv2.MSER_create()
        regions, _ = mser.detectRegions(gray)
        
        if not regions:
            return None
        
        # Convert regions to bounding boxes
        boxes = []
        for region in regions:
            x, y, w, h = cv2.boundingRect(region)
            boxes.append((x, y, w, h))
        
        if not boxes:
            return None
        
        # Find the bounding box of all text regions
        xs = [box[0] for box in boxes]
        ys = [box[1] for box in boxes]
        ws = [box[2] for box in boxes]
        hs = [box[3] for box in boxes]
        
        min_x = min(xs)
        min_y = min(ys)
        max_x = max(x + w for x, y, w, h in boxes)
        max_y = max(y + h for x, y, w, h in boxes)
        
        return AbsoluteBoundingBox(
            min_x,
            min_y,
            max_x - min_x,
            max_y - min_y
        )
    
    def _detect_by_heuristics(self, image: np.ndarray) -> Optional[AbsoluteBoundingBox]:
        """Detect table using UI heuristics for Free Fire."""
        height, width = image.shape[:2]
        
        # Free Fire typically shows the table in the center/lower portion
        # These are relative coordinates based on common UI patterns
        table_x = int(width * 0.1)
        table_y = int(height * 0.25)
        table_w = int(width * 0.8)
        table_h = int(height * 0.7)
        
        return AbsoluteBoundingBox(table_x, table_y, table_w, table_h)
    
    def _validate_bbox(self, bbox: AbsoluteBoundingBox, width: int, height: int) -> bool:
        """Validate that the detected bounding box is reasonable."""
        # Check size
        if bbox.width < width * self.min_table_width_ratio:
            return False
        if bbox.height < height * self.min_table_height_ratio:
            return False
            
        # Check position
        if bbox.x < 0 or bbox.y < 0:
            return False
        if bbox.x + bbox.width > width or bbox.y + bbox.height > height:
            return False
        
        return True
    
    def split_teams(self, table_image: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Split table into two team sections.
        
        Args:
            table_image: Cropped table image
            
        Returns:
            Tuple of (team1_image, team2_image)
        """
        height, width = table_image.shape[:2]
        
        # Typically teams are split vertically or horizontally
        # Try vertical split first (side by side)
        mid_x = width // 2
        
        team1 = table_image[:, :mid_x]
        team2 = table_image[:, mid_x:]
        
        return team1, team2
    
    def detect_rows(self, team_image: np.ndarray) -> List[AbsoluteBoundingBox]:
        """
        Detect individual player rows in a team section.
        
        Args:
            team_image: Team section image
            
        Returns:
            List of row bounding boxes
        """
        height, width = team_image.shape[:2]
        
        # Use horizontal projection to find rows
        gray = cv2.cvtColor(team_image, cv2.COLOR_RGB2GRAY)
        
        # Binarize
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Horizontal projection
        horizontal_proj = np.sum(binary, axis=1)
        
        # Find peaks (rows with content)
        threshold = np.max(horizontal_proj) * 0.3
        row_indices = np.where(horizontal_proj > threshold)[0]
        
        if len(row_indices) == 0:
            # Fallback to equal division
            row_height = height // 4
            rows = []
            for i in range(4):
                y = i * row_height
                rows.append(AbsoluteBoundingBox(0, y, width, row_height))
            return rows
        
        # Group consecutive indices into rows
        rows = []
        current_row = [row_indices[0]]
        
        for idx in row_indices[1:]:
            if idx - current_row[-1] <= 5:  # Within 5 pixels
                current_row.append(idx)
            else:
                if current_row:
                    y_min = min(current_row)
                    y_max = max(current_row)
                    rows.append(AbsoluteBoundingBox(0, y_min, width, y_max - y_min + 1))
                current_row = [idx]
        
        if current_row:
            y_min = min(current_row)
            y_max = max(current_row)
            rows.append(AbsoluteBoundingBox(0, y_min, width, y_max - y_min + 1))
        
        # Ensure we have exactly 4 rows
        if len(rows) < 4:
            # Pad with empty rows
            row_height = height // 4
            for i in range(len(rows), 4):
                y = i * row_height
                rows.append(AbsoluteBoundingBox(0, y, width, row_height))
        elif len(rows) > 4:
            # Take top 4 rows
            rows = rows[:4]
        
        return rows
