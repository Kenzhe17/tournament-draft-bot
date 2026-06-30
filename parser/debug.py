import cv2
import numpy as np
from pathlib import Path
from typing import Dict, List, Optional
from .models import AbsoluteBoundingBox


class DebugVisualizer:
    """Create debug visualizations for parsing pipeline."""
    
    COLORS = {
        'table': (0, 255, 0),      # Green
        'team': (255, 0, 0),       # Blue
        'row': (0, 165, 255),      # Orange
        'column': (255, 255, 0),   # Cyan
        'header': (255, 0, 255),   # Magenta
        'text': (255, 255, 255)    # White
    }
    
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def save_original(self, image: np.ndarray, name: str):
        """Save original image."""
        path = self.output_dir / f"{name}_original.png"
        cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    
    def save_overlay(self, image: np.ndarray, annotations: Dict, name: str):
        """
        Save image with overlay annotations.
        
        Args:
            image: Original image
            annotations: Dictionary of annotations to draw
            name: Output name
        """
        overlay = image.copy()
        
        # Draw table
        if 'table' in annotations:
            bbox = annotations['table']
            cv2.rectangle(overlay, (bbox.x, bbox.y), (bbox.x2, bbox.y2), 
                          self.COLORS['table'], 3)
        
        # Draw teams
        if 'teams' in annotations:
            for i, team_bbox in enumerate(annotations['teams']):
                cv2.rectangle(overlay, (team_bbox.x, team_bbox.y), (team_bbox.x2, team_bbox.y2),
                              self.COLORS['team'], 2)
                cv2.putText(overlay, f"Team {i+1}", (team_bbox.x, team_bbox.y - 10),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, self.COLORS['team'], 2)
        
        # Draw rows
        if 'rows' in annotations:
            for i, row_bbox in enumerate(annotations['rows']):
                cv2.rectangle(overlay, (row_bbox.x, row_bbox.y), (row_bbox.x2, row_bbox.y2),
                              self.COLORS['row'], 1)
        
        # Draw headers
        if 'headers' in annotations:
            for header_name, bbox in annotations['headers'].items():
                cv2.rectangle(overlay, (bbox.x, bbox.y), (bbox.x2, bbox.y2),
                              self.COLORS['header'], 2)
                cv2.putText(overlay, header_name, (bbox.x, bbox.y - 5),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, self.COLORS['header'], 1)
        
        # Draw columns
        if 'columns' in annotations:
            for col_name, bbox in annotations['columns'].items():
                cv2.rectangle(overlay, (bbox.x, bbox.y), (bbox.x2, bbox.y2),
                              self.COLORS['column'], 1)
        
        path = self.output_dir / f"{name}_overlay.png"
        cv2.imwrite(str(path), cv2.cvtColor(overlay, cv2.COLOR_RGB2BGR))
    
    def save_crop(self, image: np.ndarray, bbox: AbsoluteBoundingBox, name: str):
        """Save cropped region."""
        crop = image[bbox.y:bbox.y2, bbox.x:bbox.x2]
        path = self.output_dir / f"{name}.png"
        cv2.imwrite(str(path), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR))
    
    def save_crops(self, image: np.ndarray, crops: Dict[str, AbsoluteBoundingBox], prefix: str):
        """Save multiple cropped regions."""
        for name, bbox in crops.items():
            self.save_crop(image, bbox, f"{prefix}_{name}")
