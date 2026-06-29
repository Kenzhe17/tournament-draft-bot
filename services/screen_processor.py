"""Screen processor for Free Fire match result screenshots."""

import cv2
import numpy as np
import pytesseract
from typing import Dict, List, Tuple, Optional
import json
from pathlib import Path


class ScreenProcessor:
    """Process Free Fire match result screenshots using OpenCV and Tesseract."""

    # Reference resolution for normalization
    REFERENCE_WIDTH = 1920
    REFERENCE_HEIGHT = 1080

    # ROI bounding boxes for 1920x1080 resolution
    # Format: (x, y, width, height)
    ROI_CONFIG = {
        "winner_team_name": (860, 100, 200, 50),
        "left_team_name": (100, 100, 300, 50),
        "right_team_name": (1520, 100, 300, 50),
        "left_players": [
            {"nickname": (100, 200, 200, 30), "kills": (320, 200, 50, 30), "damage": (380, 200, 80, 30)},
            {"nickname": (100, 260, 200, 30), "kills": (320, 260, 50, 30), "damage": (380, 260, 80, 30)},
            {"nickname": (100, 320, 200, 30), "kills": (320, 320, 50, 30), "damage": (380, 320, 80, 30)},
            {"nickname": (100, 380, 200, 30), "kills": (320, 380, 50, 30), "damage": (380, 380, 80, 30)},
        ],
        "right_players": [
            {"nickname": (1420, 200, 200, 30), "kills": (1640, 200, 50, 30), "damage": (1700, 200, 80, 30)},
            {"nickname": (1420, 260, 200, 30), "kills": (1640, 260, 50, 30), "damage": (1700, 260, 80, 30)},
            {"nickname": (1420, 320, 200, 30), "kills": (1640, 320, 50, 30), "damage": (1700, 320, 80, 30)},
            {"nickname": (1420, 380, 200, 30), "kills": (1640, 380, 50, 30), "damage": (1700, 380, 80, 30)},
        ],
    }

    def __init__(self, anchor_image_path: Optional[str] = None):
        """
        Initialize the screen processor.

        Args:
            anchor_image_path: Path to the VS logo reference image
        """
        self.anchor_image = None
        if anchor_image_path:
            self.anchor_image = cv2.imread(anchor_image_path, cv2.IMREAD_GRAYSCALE)

    def find_scale_factor(self, image: np.ndarray) -> float:
        """
        Find the scale factor by matching the VS logo anchor.

        Uses a scale pyramid from 0.5 to 1.5 to find the best match.

        Args:
            image: Input image (BGR format)

        Returns:
            Scale factor to normalize to 1920x1080
        """
        if self.anchor_image is None:
            # If no anchor image, assume original resolution
            return self.REFERENCE_WIDTH / image.shape[1]

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        anchor_h, anchor_w = self.anchor_image.shape

        best_scale = 1.0
        best_match_val = -1

        # Scale pyramid from 0.5 to 1.5
        for scale in np.arange(0.5, 1.6, 0.1):
            scaled_anchor = cv2.resize(
                self.anchor_image,
                (int(anchor_w * scale), int(anchor_h * scale))
            )

            if scaled_anchor.shape[0] > gray.shape[0] or scaled_anchor.shape[1] > gray.shape[1]:
                continue

            # Template matching
            result = cv2.matchTemplate(gray, scaled_anchor, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)

            if max_val > best_match_val:
                best_match_val = max_val
                best_scale = scale

        # Calculate scale factor to normalize to reference resolution
        # If the anchor was found at scale X, the image needs to be scaled by 1/X
        # to match the reference resolution where the anchor is at its original size
        return 1.0 / best_scale

    def normalize_image(self, image: np.ndarray, scale_factor: float) -> np.ndarray:
        """
        Normalize image to 1920x1080 resolution.

        Args:
            image: Input image
            scale_factor: Scale factor from find_scale_factor

        Returns:
            Normalized image at 1920x1080
        """
        new_width = int(image.shape[1] * scale_factor)
        new_height = int(image.shape[0] * scale_factor)

        resized = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LINEAR)

        # Crop or pad to exact 1920x1080
        if new_width > self.REFERENCE_WIDTH:
            x_start = (new_width - self.REFERENCE_WIDTH) // 2
            resized = resized[:, x_start:x_start + self.REFERENCE_WIDTH]
        elif new_width < self.REFERENCE_WIDTH:
            pad_width = self.REFERENCE_WIDTH - new_width
            resized = cv2.copyMakeBorder(
                resized, 0, 0, pad_width // 2, pad_width - pad_width // 2,
                cv2.BORDER_CONSTANT, value=(0, 0, 0)
            )

        if new_height > self.REFERENCE_HEIGHT:
            y_start = (new_height - self.REFERENCE_HEIGHT) // 2
            resized = resized[y_start:y_start + self.REFERENCE_HEIGHT, :]
        elif new_height < self.REFERENCE_HEIGHT:
            pad_height = self.REFERENCE_HEIGHT - new_height
            resized = cv2.copyMakeBorder(
                resized, pad_height // 2, pad_height - pad_height // 2, 0, 0,
                cv2.BORDER_CONSTANT, value=(0, 0, 0)
            )

        return resized

    def crop_roi(self, image: np.ndarray, roi: Tuple[int, int, int, int]) -> np.ndarray:
        """
        Crop a region of interest from the image.

        Args:
            image: Input image
            roi: (x, y, width, height)

        Returns:
            Cropped image
        """
        x, y, w, h = roi
        return image[y:y+h, x:x+w]

    def preprocess_for_ocr(self, image: np.ndarray, is_number: bool = False) -> np.ndarray:
        """
        Preprocess image for OCR with grayscale and Otsu thresholding.

        Args:
            image: Input image (BGR)
            is_number: Whether this is a number field (kills/damage)

        Returns:
            Preprocessed grayscale image
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # Apply Gaussian blur to reduce noise
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        # Otsu's thresholding
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        return binary

    def extract_text(self, image: np.ndarray, is_number: bool = False) -> str:
        """
        Extract text from image using Tesseract OCR.

        Args:
            image: Input image (BGR)
            is_number: Whether this is a number field (kills/damage)

        Returns:
            Extracted text
        """
        preprocessed = self.preprocess_for_ocr(image, is_number)

        config = '--psm 6'
        if is_number:
            config += ' -c tessedit_char_whitelist=0123456789/'

        text = pytesseract.image_to_string(preprocessed, config=config).strip()

        return text

    def extract_number(self, image: np.ndarray) -> int:
        """
        Extract number from image using Tesseract OCR.

        Args:
            image: Input image (BGR)

        Returns:
            Extracted number (0 if parsing fails)
        """
        text = self.extract_text(image, is_number=True)

        try:
            # Handle format like "10/5" - extract first number
            if '/' in text:
                text = text.split('/')[0]
            return int(text)
        except (ValueError, IndexError):
            return 0

    def process_screenshot(self, image_path: str) -> Dict:
        """
        Process a Free Fire match result screenshot.

        Args:
            image_path: Path to the screenshot image

        Returns:
            Dictionary with match data in the specified JSON format
        """
        image = cv2.imread(image_path)
        if image is None:
            raise ValueError(f"Could not read image from {image_path}")

        # Find scale factor and normalize
        scale_factor = self.find_scale_factor(image)
        normalized = self.normalize_image(image, scale_factor)

        # Extract winner team name
        winner_roi = self.crop_roi(normalized, self.ROI_CONFIG["winner_team_name"])
        winner_name = self.extract_text(winner_roi, is_number=False)

        # Extract left team name
        left_team_roi = self.crop_roi(normalized, self.ROI_CONFIG["left_team_name"])
        left_team_name = self.extract_text(left_team_roi, is_number=False)

        # Extract right team name
        right_team_roi = self.crop_roi(normalized, self.ROI_CONFIG["right_team_name"])
        right_team_name = self.extract_text(right_team_roi, is_number=False)

        # Extract left team players
        left_players = []
        for player_config in self.ROI_CONFIG["left_players"]:
            nickname_roi = self.crop_roi(normalized, player_config["nickname"])
            kills_roi = self.crop_roi(normalized, player_config["kills"])
            damage_roi = self.crop_roi(normalized, player_config["damage"])

            nickname = self.extract_text(nickname_roi, is_number=False)
            kills = self.extract_number(kills_roi)
            damage = self.extract_number(damage_roi)

            if nickname:  # Only add if nickname was found
                left_players.append({
                    "nickname": nickname,
                    "kills": kills,
                    "damage": damage
                })

        # Extract right team players
        right_players = []
        for player_config in self.ROI_CONFIG["right_players"]:
            nickname_roi = self.crop_roi(normalized, player_config["nickname"])
            kills_roi = self.crop_roi(normalized, player_config["kills"])
            damage_roi = self.crop_roi(normalized, player_config["damage"])

            nickname = self.extract_text(nickname_roi, is_number=False)
            kills = self.extract_number(kills_roi)
            damage = self.extract_number(damage_roi)

            if nickname:  # Only add if nickname was found
                right_players.append({
                    "nickname": nickname,
                    "kills": kills,
                    "damage": damage
                })

        # Build result
        result = {
            "winner": winner_name,
            "teams": [
                {
                    "name": left_team_name,
                    "players": left_players
                },
                {
                    "name": right_team_name,
                    "players": right_players
                }
            ]
        }

        return result

    def process_screenshot_from_bytes(self, image_bytes: bytes) -> Dict:
        """
        Process a screenshot from bytes (e.g., from Discord attachment).

        Args:
            image_bytes: Image data as bytes

        Returns:
            Dictionary with match data in the specified JSON format
        """
        nparr = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if image is None:
            raise ValueError("Could not decode image from bytes")

        # Find scale factor and normalize
        scale_factor = self.find_scale_factor(image)
        normalized = self.normalize_image(image, scale_factor)

        # Extract winner team name
        winner_roi = self.crop_roi(normalized, self.ROI_CONFIG["winner_team_name"])
        winner_name = self.extract_text(winner_roi, is_number=False)

        # Extract left team name
        left_team_roi = self.crop_roi(normalized, self.ROI_CONFIG["left_team_name"])
        left_team_name = self.extract_text(left_team_roi, is_number=False)

        # Extract right team name
        right_team_roi = self.crop_roi(normalized, self.ROI_CONFIG["right_team_name"])
        right_team_name = self.extract_text(right_team_roi, is_number=False)

        # Extract left team players
        left_players = []
        for player_config in self.ROI_CONFIG["left_players"]:
            nickname_roi = self.crop_roi(normalized, player_config["nickname"])
            kills_roi = self.crop_roi(normalized, player_config["kills"])
            damage_roi = self.crop_roi(normalized, player_config["damage"])

            nickname = self.extract_text(nickname_roi, is_number=False)
            kills = self.extract_number(kills_roi)
            damage = self.extract_number(damage_roi)

            if nickname:
                left_players.append({
                    "nickname": nickname,
                    "kills": kills,
                    "damage": damage
                })

        # Extract right team players
        right_players = []
        for player_config in self.ROI_CONFIG["right_players"]:
            nickname_roi = self.crop_roi(normalized, player_config["nickname"])
            kills_roi = self.crop_roi(normalized, player_config["kills"])
            damage_roi = self.crop_roi(normalized, player_config["damage"])

            nickname = self.extract_text(nickname_roi, is_number=False)
            kills = self.extract_number(kills_roi)
            damage = self.extract_number(damage_roi)

            if nickname:
                right_players.append({
                    "nickname": nickname,
                    "kills": kills,
                    "damage": damage
                })

        # Build result
        result = {
            "winner": winner_name,
            "teams": [
                {
                    "name": left_team_name,
                    "players": left_players
                },
                {
                    "name": right_team_name,
                    "players": right_players
                }
            ]
        }

        return result
