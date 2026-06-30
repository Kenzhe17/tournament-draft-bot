import cv2
import numpy as np
from typing import Tuple


class ImagePreprocessor:
    """Image preprocessing for OCR and detection."""
    
    @staticmethod
    def denoise(image: np.ndarray) -> np.ndarray:
        """
        Remove noise from image.
        
        Args:
            image: Input image
            
        Returns:
            Denoised image
        """
        return cv2.fastNlMeansDenoisingColored(image, None, 10, 10, 7, 21)
    
    @staticmethod
    def enhance_contrast(image: np.ndarray) -> np.ndarray:
        """
        Enhance image contrast using CLAHE.
        
        Args:
            image: Input image
            
        Returns:
            Contrast-enhanced image
        """
        # Convert to LAB color space
        lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
        l, a, b = cv2.split(lab)
        
        # Apply CLAHE to L channel
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        
        # Merge back
        lab = cv2.merge([l, a, b])
        
        return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    
    @staticmethod
    def sharpen(image: np.ndarray) -> np.ndarray:
        """
        Sharpen image for better text detection.
        
        Args:
            image: Input image
            
        Returns:
            Sharpened image
        """
        kernel = np.array([[-1, -1, -1],
                          [-1,  9, -1],
                          [-1, -1, -1]])
        
        return cv2.filter2D(image, -1, kernel)
    
    @staticmethod
    def to_grayscale(image: np.ndarray) -> np.ndarray:
        """
        Convert image to grayscale.
        
        Args:
            image: Input image
            
        Returns:
            Grayscale image
        """
        return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    
    @staticmethod
    def binarize(image: np.ndarray, threshold: int = 127) -> np.ndarray:
        """
        Binarize image using Otsu's method or fixed threshold.
        
        Args:
            image: Input grayscale image
            threshold: Fixed threshold (if None, use Otsu)
            
        Returns:
            Binary image
        """
        if threshold is None:
            _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            _, binary = cv2.threshold(image, threshold, 255, cv2.THRESH_BINARY)
        
        return binary
    
    @staticmethod
    def invert(image: np.ndarray) -> np.ndarray:
        """
        Invert image colors (for dark text on light background).
        
        Args:
            image: Input image
            
        Returns:
            Inverted image
        """
        return 255 - image
    
    @staticmethod
    def preprocess_for_ocr(image: np.ndarray) -> np.ndarray:
        """
        Full preprocessing pipeline optimized for OCR.
        
        Args:
            image: Input image
            
        Returns:
            Preprocessed image
        """
        # Denoise
        processed = ImagePreprocessor.denoise(image)
        
        # Enhance contrast
        processed = ImagePreprocessor.enhance_contrast(processed)
        
        # Sharpen
        processed = ImagePreprocessor.sharpen(processed)
        
        return processed
    
    @staticmethod
    def preprocess_for_detection(image: np.ndarray) -> np.ndarray:
        """
        Full preprocessing pipeline optimized for detection.
        
        Args:
            image: Input image
            
        Returns:
            Preprocessed image
        """
        # Denoise
        processed = ImagePreprocessor.denoise(image)
        
        # Enhance contrast
        processed = ImagePreprocessor.enhance_contrast(processed)
        
        return processed
    
    @staticmethod
    def resize_for_ocr(image: np.ndarray, scale: float = 2.0) -> np.ndarray:
        """
        Resize image for better OCR accuracy.
        
        Args:
            image: Input image
            scale: Scale factor
            
        Returns:
            Resized image
        """
        height, width = image.shape[:2]
        new_width = int(width * scale)
        new_height = int(height * scale)
        
        return cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
