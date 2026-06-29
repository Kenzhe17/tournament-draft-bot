"""Service for analyzing Free Fire match screenshots using OCR."""

import cv2
import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
import re
import logging
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


@dataclass
class PlayerStats:
    """Player statistics from screenshot."""
    nickname: str
    kills: int
    deaths: int
    assists: int
    damage: int
    confidence: float = 100.0  # OCR confidence percentage


@dataclass
class MatchResult:
    """Match result from screenshot."""
    winner_team: str
    loser_team: str
    score: str
    team1_players: List[PlayerStats]
    team2_players: List[PlayerStats]


class ImageAnalyzer:
    """Analyzer for Free Fire match screenshots."""

    def __init__(self):
        self.reader = None  # EasyOCR reader, initialized lazily

    def _get_reader(self):
        """Lazy load EasyOCR reader."""
        if self.reader is None:
            import easyocr
            self.reader = easyocr.Reader(['en'], gpu=False)
        return self.reader

    def analyze_screenshot(self, image_path: str, expected_players: List[str]) -> Optional[MatchResult]:
        """
        Analyze a Free Fire match screenshot.

        Args:
            image_path: Path to the screenshot image
            expected_players: List of expected player nicknames for this match

        Returns:
            MatchResult if successful, None if analysis fails
        """
        try:
            # Load image
            image = cv2.imread(image_path)
            if image is None:
                raise ValueError(f"Failed to load image: {image_path}")

            # Detect regions
            score_region = self._detect_score_region(image)
            team1_region = self._detect_team_region(image, team=1)
            team2_region = self._detect_team_region(image, team=2)

            if score_region is None or team1_region is None or team2_region is None:
                raise ValueError("Failed to detect required regions")

            # Extract text from regions
            score_text = self._extract_text(score_region)
            team1_text = self._extract_text(team1_region)
            team2_text = self._extract_text(team2_region)

            # Parse score
            score = self._parse_score(score_text)
            if not score:
                raise ValueError("Failed to parse score")

            # Parse player stats
            team1_players = self._parse_player_stats(team1_text, expected_players)
            team2_players = self._parse_player_stats(team2_text, expected_players)

            # Determine winner based on score
            winner_team, loser_team = self._determine_winner(score)

            return MatchResult(
                winner_team=winner_team,
                loser_team=loser_team,
                score=score,
                team1_players=team1_players,
                team2_players=team2_players
            )

        except Exception as e:
            print(f"Error analyzing screenshot: {e}")
            return None

    def _detect_score_region(self, image: np.ndarray) -> Optional[np.ndarray]:
        """
        Detect the score region in the image.

        The score is typically at the top center of the screen.
        """
        height, width = image.shape[:2]

        # Score region: top center (adjust these coordinates based on actual UI)
        score_y_start = int(height * 0.1)
        score_y_end = int(height * 0.2)
        score_x_start = int(width * 0.3)
        score_x_end = int(width * 0.7)

        score_region = image[score_y_start:score_y_end, score_x_start:score_x_end]
        return score_region

    def _detect_team_region(self, image: np.ndarray, team: int) -> Optional[np.ndarray]:
        """
        Detect team region (left or right side).

        Args:
            image: Full screenshot
            team: 1 for left team, 2 for right team
        """
        height, width = image.shape[:2]

        if team == 1:
            # Left team region
            y_start = int(height * 0.25)
            y_end = int(height * 0.85)
            x_start = int(width * 0.05)
            x_end = int(width * 0.45)
        else:
            # Right team region
            y_start = int(height * 0.25)
            y_end = int(height * 0.85)
            x_start = int(width * 0.55)
            x_end = int(width * 0.95)

        team_region = image[y_start:y_end, x_start:x_end]
        return team_region

    def _extract_text(self, region: np.ndarray) -> str:
        """
        Extract text from image region using EasyOCR.
        """
        reader = self._get_reader()
        results = reader.readtext(region)

        # Log only summary info
        logger.info(f"OCR detected {len(results)} text blocks")

        # Combine all detected text
        text_lines = [result[1] for result in results]
        combined_text = '\n'.join(text_lines)

        # Only log combined text in DEBUG mode
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"Combined OCR text: '{combined_text}'")

        return combined_text

    def _parse_score(self, text: str) -> Optional[str]:
        """
        Parse score from text with multiple format support.

        Expected formats: "4 - 2", "4-2", "4 : 2", "4/2", "4 2", etc.
        Also handles OCR errors like O instead of 0, l instead 1.
        """
        # Normalize text - remove extra spaces, common OCR errors
        normalized = text.strip()
        # Replace common OCR mistakes
        normalized = normalized.replace('O', '0').replace('o', '0')
        normalized = normalized.replace('l', '1').replace('I', '1')
        normalized = normalized.replace(':', '-').replace('/', '-')
        # Remove extra spaces around dash
        normalized = re.sub(r'\s*-\s*', '-', normalized)

        # Try multiple patterns
        patterns = [
            r'(\d+)-(\d+)',  # 4-2
            r'(\d+)\s+(\d+)',  # 4 2
            r'(\d+):(\d+)',  # 4:2
            r'(\d+)/(\d+)',  # 4/2
        ]

        for pattern in patterns:
            match = re.search(pattern, normalized)
            if match:
                score1 = match.group(1)
                score2 = match.group(2)
                result = f"{score1} - {score2}"
                logger.info(f"Score parsed successfully: {result}")
                return result

        # If no pattern matched, try to find any two numbers
        numbers = re.findall(r'\d+', normalized)
        if len(numbers) >= 2:
            result = f"{numbers[0]} - {numbers[1]}"
            logger.info(f"Score parsed from numbers: {result}")
            return result

        logger.warning("Failed to parse score from OCR text")
        return None

    def _parse_player_stats(self, text: str, expected_players: List[str]) -> List[PlayerStats]:
        """
        Parse player statistics from text.

        Args:
            text: OCR text from team region
            expected_players: List of expected player names for fuzzy matching

        Returns:
            List of PlayerStats
        """
        players = []
        lines = text.split('\n')

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try to extract player stats from line
            # Pattern: nickname kills deaths assists damage
            stats = self._extract_stats_from_line(line, expected_players)
            if stats:
                players.append(stats)

        return players

    def _extract_stats_from_line(self, line: str, expected_players: List[str]) -> Optional[PlayerStats]:
        """
        Extract player stats from a single line of text.

        Args:
            line: Text line from OCR
            expected_players: List of expected player names

        Returns:
            PlayerStats if successful, None otherwise
        """
        # Try to match nickname with expected players using fuzzy matching
        nickname, confidence = self._match_nickname(line, expected_players)
        if not nickname:
            return None

        # Extract numbers from line (kills, deaths, assists, damage)
        numbers = re.findall(r'\d+', line)
        if len(numbers) < 2:  # At least kills and deaths
            return None

        try:
            kills = int(numbers[0]) if len(numbers) > 0 else 0
            deaths = int(numbers[1]) if len(numbers) > 1 else 0
            assists = int(numbers[2]) if len(numbers) > 2 else 0
            damage = int(numbers[3]) if len(numbers) > 3 else 0

            return PlayerStats(
                nickname=nickname,
                kills=kills,
                deaths=deaths,
                assists=assists,
                damage=damage,
                confidence=confidence
            )
        except (ValueError, IndexError):
            return None

    def _match_nickname(self, line: str, expected_players: List[str]) -> Tuple[Optional[str], float]:
        """
        Match nickname from line using fuzzy matching.

        Args:
            line: Text line from OCR
            expected_players: List of expected player names

        Returns:
            Tuple of (matched_nickname, confidence_percentage)
        """
        # Normalize line for comparison
        normalized_line = self._normalize_nickname(line)

        # Try exact match first
        for player in expected_players:
            normalized_player = self._normalize_nickname(player)
            if normalized_player == normalized_line:
                return player, 100.0

        # Try fuzzy matching
        result = process.extractOne(
            normalized_line,
            [self._normalize_nickname(p) for p in expected_players],
            scorer=fuzz.WRatio
        )

        if result:
            confidence = result[1]
            # Find original player name
            idx = result[2]
            return expected_players[idx], confidence

        return None, 0.0

    def _normalize_nickname(self, nickname: str) -> str:
        """
        Normalize nickname for comparison.

        Removes decorative characters, spaces, and converts to lowercase.
        """
        # Remove decorative characters
        normalized = re.sub(r'[^\w]', '', nickname)
        # Convert to lowercase
        normalized = normalized.lower()
        return normalized

    def _determine_winner(self, score: str) -> Tuple[str, str]:
        """
        Determine winner and loser from score string.

        Args:
            score: Score string like "4 - 2"

        Returns:
            Tuple of (winner_team, loser_team)
        """
        parts = score.split(' - ')
        if len(parts) != 2:
            return "Team 1", "Team 2"

        try:
            score1 = int(parts[0].strip())
            score2 = int(parts[1].strip())

            if score1 > score2:
                return "Team 1", "Team 2"
            else:
                return "Team 2", "Team 1"
        except ValueError:
            return "Team 1", "Team 2"


# Global instance
image_analyzer = ImageAnalyzer()
