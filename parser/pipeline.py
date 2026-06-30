import time
import json
from pathlib import Path
from typing import Optional
import numpy as np

from .image_loader import ImageLoader
from .preprocess import ImagePreprocessor
from .table_detector import TableDetector
from .header_detector import HeaderDetector
from .row_detector import RowDetector
from .column_detector import ColumnDetector
from .nickname_reader import NicknameReader
from .kda_reader import KDAReader
from .damage_reader import DamageReader
from .winner_reader import WinnerReader
from .score_reader import ScoreReader
from .validator import ResultValidator
from .debug import DebugVisualizer
from .models import (
    ParseResult, MatchInfo, Team, PlayerStats, 
    Score, ImageMetadata, ProcessingMetadata, AbsoluteBoundingBox
)


class ParserPipeline:
    """Main pipeline for parsing Free Fire match statistics."""
    
    def __init__(self, debug_dir: Optional[str] = None):
        self.debug_dir = debug_dir
        self.debug = DebugVisualizer(debug_dir) if debug_dir else None
        
        # Initialize components
        self.table_detector = TableDetector()
        self.header_detector = HeaderDetector()
        self.row_detector = RowDetector()
        self.column_detector = ColumnDetector()
        self.nickname_reader = NicknameReader()
        self.kda_reader = KDAReader()
        self.damage_reader = DamageReader()
        self.winner_reader = WinnerReader()
        self.score_reader = ScoreReader()
        self.validator = ResultValidator()
    
    def parse(self, image_path: str) -> ParseResult:
        """
        Parse a Free Fire match screenshot.
        
        Args:
            image_path: Path to the screenshot
            
        Returns:
            ParseResult with match data
        """
        start_time = time.time()
        
        try:
            # Step 1: Load image
            image, metadata = ImageLoader.load(image_path)
            
            if self.debug:
                self.debug.save_original(image, Path(image_path).stem)
            
            # Step 2: Preprocess
            processed = ImagePreprocessor.preprocess_for_detection(image)
            
            # Step 3: Detect table
            table_bbox = self.table_detector.detect(processed)
            if not table_bbox:
                return ParseResult(
                    success=False,
                    match=MatchInfo(),
                    teams=[],
                    image=metadata,
                    processing=ProcessingMetadata(time_ms=(time.time() - start_time) * 1000),
                    error="Failed to detect table"
                )
            
            table_image = ImageLoader.crop(processed, table_bbox)
            
            # Step 4: Detect headers
            headers = self.header_detector.detect(table_image)
            
            # Step 5: Split into teams
            team1_image, team2_image = self.table_detector.split_teams(table_image)
            
            # Step 6: Detect rows for each team
            team1_rows = self.row_detector.detect(team1_image)
            team2_rows = self.row_detector.detect(team2_image)
            
            # Step 7: Parse players
            team1 = self._parse_team(team1_image, team1_rows, headers, table_bbox, team_id=1)
            team2 = self._parse_team(team2_image, team2_rows, headers, table_bbox, team_id=2)
            
            # Step 8: Read score
            score = self._read_score(image)
            
            # Step 9: Determine winner
            winner = self._determine_winner(image, score)
            
            # Step 10: Build result
            match_info = MatchInfo(
                mode="Battle Squad",
                map="Bermuda",  # Default, could be detected
                winner=winner,
                winner_name=team1.name if winner == 1 else (team2.name if winner == 2 else ""),
                score=score
            )
            
            result = ParseResult(
                success=True,
                match=match_info,
                teams=[team1, team2],
                image=metadata,
                processing=ProcessingMetadata(time_ms=(time.time() - start_time) * 1000)
            )
            
            # Step 11: Validate
            if not self.validator.validate(result):
                errors = self.validator.get_validation_errors(result)
                result.success = False
                result.error = f"Validation failed: {', '.join(errors)}"
            
            # Step 12: Debug output
            if self.debug:
                self._save_debug(image, table_bbox, team1_image, team2_image, 
                                team1_rows, team2_rows, headers, Path(image_path).stem)
            
            return result
            
        except Exception as e:
            return ParseResult(
                success=False,
                match=MatchInfo(),
                teams=[],
                image=ImageMetadata(0, 0, "unknown"),
                processing=ProcessingMetadata(time_ms=(time.time() - start_time) * 1000),
                error=str(e)
            )
    
    def _parse_team(self, team_image: np.ndarray, rows: list, 
                    headers: dict, table_bbox: AbsoluteBoundingBox, team_id: int) -> Team:
        """Parse a single team's data."""
        players = []
        
        for i, row_bbox in enumerate(rows):
            # Get column regions for this row
            columns = self.column_detector.get_columns(headers, row_bbox)
            
            # Crop each column
            row_image = team_image[row_bbox.y:row_bbox.y2, row_bbox.x:row_bbox.x2]
            
            # Read nickname
            nickname_bbox = columns.get('name')
            nickname_image = row_image[nickname_bbox.y:nickname_bbox.y2, 
                                      nickname_bbox.x:nickname_bbox.x2] if nickname_bbox else np.array([])
            nickname_result = self.nickname_reader.read(nickname_image)
            
            # Read KDA
            kda_bbox = columns.get('kda')
            kda_image = row_image[kda_bbox.y:kda_bbox.y2, 
                                  kda_bbox.x:kda_bbox.x2] if kda_bbox else np.array([])
            kills, deaths, assists, kda_conf = self.kda_reader.read(kda_image)
            
            # Read damage
            dmg_bbox = columns.get('dmg')
            dmg_image = row_image[dmg_bbox.y:dmg_bbox.y2, 
                                  dmg_bbox.x:dmg_bbox.x2] if dmg_bbox else np.array([])
            damage, dmg_conf = self.damage_reader.read(dmg_image)
            
            player = PlayerStats(
                position=i + 1,
                nickname=nickname_result.text,
                nickname_confidence=nickname_result.confidence,
                kills=kills,
                kills_confidence=kda_conf,
                deaths=deaths,
                deaths_confidence=kda_conf,
                assists=assists,
                assists_confidence=kda_conf,
                damage=damage,
                damage_confidence=dmg_conf
            )
            
            players.append(player)
        
        # Team name (could be detected from UI, using placeholder for now)
        team_name = f"TEAM{team_id}"
        
        return Team(id=team_id, name=team_name, players=players)
    
    def _read_score(self, image: np.ndarray) -> Optional[Score]:
        """Read match score."""
        score_region = self.score_reader.find_score_region(image)
        if score_region:
            x, y, w, h = score_region
            score_image = image[y:y+h, x:x+w]
            return self.score_reader.read(score_image)
        return None
    
    def _determine_winner(self, image: np.ndarray, score: Optional[Score]) -> int:
        """Determine winning team."""
        # Try to read from image first
        winner = self.winner_reader.read(image)
        if winner:
            return winner
        
        # Fallback to score
        if score:
            return self.winner_reader.read_from_score((score.team1, score.team2))
        
        return 0
    
    def _save_debug(self, image: np.ndarray, table_bbox: AbsoluteBoundingBox,
                    team1_image: np.ndarray, team2_image: np.ndarray,
                    team1_rows: list, team2_rows: list, headers: dict, name: str):
        """Save debug visualizations."""
        if not self.debug:
            return
        
        # Save overlay
        annotations = {
            'table': table_bbox,
            'headers': headers
        }
        self.debug.save_overlay(image, annotations, name)
        
        # Save table crop
        self.debug.save_crop(image, table_bbox, f"{name}_table")
        
        # Save team crops
        # (Note: team images are already cropped from table, so we save them directly)
        # This would need adjustment in a real implementation
    
    def save_result(self, result: ParseResult, output_path: str):
        """Save parse result as JSON."""
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
