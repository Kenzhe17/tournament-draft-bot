import cv2
import numpy as np
from pathlib import Path
from typing import Tuple, Optional
from .models import ImageMetadata


class ImageLoader:
    """Load and validate images with multi-resolution support."""
    
    SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}
    
    @staticmethod
    def load(image_path: str) -> Tuple[np.ndarray, ImageMetadata]:
        """
        Load image from path.
        
        Args:
            image_path: Path to image file
            
        Returns:
            Tuple of (image_array, metadata)
        """
        path = Path(image_path)
        
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")
        
        if path.suffix.lower() not in ImageLoader.SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {path.suffix}")
        
        image = cv2.imread(str(path))
        
        if image is None:
            raise ValueError(f"Failed to load image: {image_path}")
        
        # Convert BGR to RGB for consistency
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        
        height, width = image.shape[:2]
        
        metadata = ImageMetadata(
            width=width,
            height=height,
            format=path.suffix.lower()[1:]
        )
        
        return image, metadata
    
    @staticmethod
    def get_resolution_category(width: int, height: int) -> str:
        """
        Categorize image resolution.
        
        Args:
            width: Image width
            height: Image height
            
        Returns:
            Resolution category string
        """
        min_dim = min(width, height)
        
        if min_dim >= 2160:
            return "4K"
        elif min_dim >= 1440:
            return "2K"
        elif min_dim >= 1080:
            return "1440p"
        elif min_dim >= 720:
            return "1080p"
        elif min_dim >= 480:
            return "720p"
        else:
            return "SD"
    
    @staticmethod
    def normalize_resolution(image: np.ndarray, target_width: int = 1920) -> np.ndarray:
        """
        Normalize image to target width while maintaining aspect ratio.
        
        Args:
            image: Input image
            target_width: Target width in pixels
            
        Returns:
            Resized image
        """
        height, width = image.shape[:2]
        
        if width == target_width:
            return image
        
        scale = target_width / width
        new_height = int(height * scale)
        
        resized = cv2.resize(image, (target_width, new_height), interpolation=cv2.INTER_AREA)
        
        return resized
    
    @staticmethod
    def crop(image: np.ndarray, bbox: 'AbsoluteBoundingBox') -> np.ndarray:
        """
        Crop image using bounding box.
        
        Args:
            image: Input image
            bbox: Absolute bounding box
            
        Returns:
            Cropped image
        """
        x1 = max(0, bbox.x)
        y1 = max(0, bbox.y)
        x2 = min(image.shape[1], bbox.x2)
        y2 = min(image.shape[0], bbox.y2)
        
        if x2 <= x1 or y2 <= y1:
            raise ValueError(f"Invalid crop coordinates: {bbox}")
        
        return image[y1:y2, x1:x2]
