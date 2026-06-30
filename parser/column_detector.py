import cv2
import numpy as np
from typing import Dict
from .models import AbsoluteBoundingBox


class ColumnDetector:
    """Detect column regions based on header positions."""
    
    def __init__(self):
        pass
    
    def get_columns(self, headers: Dict[str, AbsoluteBoundingBox], 
                    row_bbox: AbsoluteBoundingBox) -> Dict[str, AbsoluteBoundingBox]:
        """
        Get column regions for a specific row.
        
        Args:
            headers: Header bounding boxes
            row_bbox: Row bounding box
            
        Returns:
            Dictionary mapping column names to their bounding boxes
        """
        columns = {}
        
        row_y = row_bbox.y
        row_height = row_bbox.height
        
        for header_name, header_bbox in headers.items():
            columns[header_name] = AbsoluteBoundingBox(
                header_bbox.x,
                row_y,
                header_bbox.width,
                row_height
            )
        
        return columns
    
    def refine_columns(self, columns: Dict[str, AbsoluteBoundingBox], 
                       image: np.ndarray) -> Dict[str, AbsoluteBoundingBox]:
        """
        Refine column boundaries using image content.
        
        Args:
            columns: Initial column bounding boxes
            image: Row image
            
        Returns:
            Refined column bounding boxes
        """
        refined = {}
        
        for col_name, bbox in columns.items():
            # Crop column
            col_image = image[bbox.y:bbox.y2, bbox.x:bbox.x2]
            
            if col_image.size == 0:
                refined[col_name] = bbox
                continue
            
            # Find actual content bounds
            gray = cv2.cvtColor(col_image, cv2.COLOR_RGB2GRAY)
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            
            # Find non-empty pixels
            rows = np.any(binary < 128, axis=1)
            cols = np.any(binary < 128, axis=0)
            
            if np.any(rows) and np.any(cols):
                row_indices = np.where(rows)[0]
                col_indices = np.where(cols)[0]
                
                y_offset = row_indices[0]
                height = row_indices[-1] - row_indices[0] + 1
                x_offset = col_indices[0]
                width = col_indices[-1] - col_indices[0] + 1
                
                refined[col_name] = AbsoluteBoundingBox(
                    bbox.x + x_offset,
                    bbox.y + y_offset,
                    width,
                    height
                )
            else:
                refined[col_name] = bbox
        
        return refined
