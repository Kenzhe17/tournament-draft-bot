from dataclasses import dataclass
from typing import Optional, List
from pydantic import BaseModel, Field


@dataclass
class BoundingBox:
    x: float
    y: float
    width: float
    height: float
    
    def to_absolute(self, image_width: int, image_height: int) -> 'AbsoluteBoundingBox':
        return AbsoluteBoundingBox(
            x=int(self.x * image_width),
            y=int(self.y * image_height),
            width=int(self.width * image_width),
            height=int(self.height * image_height)
        )


@dataclass
class AbsoluteBoundingBox:
    x: int
    y: int
    width: int
    height: int
    
    @property
    def x2(self) -> int:
        return self.x + self.width
    
    @property
    def y2(self) -> int:
        return self.y + self.height
    
    def to_relative(self, image_width: int, image_height: int) -> BoundingBox:
        return BoundingBox(
            x=self.x / image_width,
            y=self.y / image_height,
            width=self.width / image_width,
            height=self.height / image_height
        )


@dataclass
class OCRResult:
    text: str
    confidence: float
    
    def is_valid(self) -> bool:
        return self.confidence > 0.5 and len(self.text.strip()) > 0


@dataclass
class PlayerStats:
    position: int
    nickname: str
    nickname_confidence: float
    kills: int
    kills_confidence: float
    deaths: int
    deaths_confidence: float
    assists: int
    assists_confidence: float
    damage: int
    damage_confidence: float
    
    def is_valid(self) -> bool:
        return (
            self.nickname_confidence > 0.5
            and self.kills_confidence > 0.5
            and self.deaths_confidence > 0.5
            and self.assists_confidence > 0.5
            and self.damage_confidence > 0.5
        )


@dataclass
class Team:
    id: int
    name: str
    players: List[PlayerStats]
    
    def is_valid(self) -> bool:
        return len(self.players) == 4 and all(p.is_valid() for p in self.players)


@dataclass
class Score:
    team1: int
    team2: int
    
    def get_winner(self) -> int:
        if self.team1 > self.team2:
            return 1
        elif self.team2 > self.team1:
            return 2
        return 0


@dataclass
class MatchInfo:
    mode: str = "Battle Squad"
    map: str = "Unknown"
    winner: int = 0
    winner_name: str = ""
    score: Optional[Score] = None


@dataclass
class ImageMetadata:
    width: int
    height: int
    format: str


@dataclass
class ProcessingMetadata:
    time_ms: float
    engine: str = "OpenCV + PaddleOCR"


@dataclass
class ParseResult:
    success: bool
    match: MatchInfo
    teams: List[Team]
    image: ImageMetadata
    processing: ProcessingMetadata
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        if not self.success:
            return {
                "success": False,
                "error": self.error,
                "image": {
                    "width": self.image.width,
                    "height": self.image.height
                },
                "processing": {
                    "time_ms": self.processing.time_ms,
                    "engine": self.processing.engine
                }
            }
        
        return {
            "success": True,
            "match": {
                "mode": self.match.mode,
                "map": self.match.map,
                "winner": self.match.winner,
                "winner_name": self.match.winner_name,
                "score": {
                    "team1": self.match.score.team1,
                    "team2": self.match.score.team2
                } if self.match.score else None
            },
            "teams": [
                {
                    "id": team.id,
                    "name": team.name,
                    "players": [
                        {
                            "position": player.position,
                            "nickname": player.nickname,
                            "nickname_confidence": player.nickname_confidence,
                            "kills": player.kills,
                            "kills_confidence": player.kills_confidence,
                            "deaths": player.deaths,
                            "deaths_confidence": player.deaths_confidence,
                            "assists": player.assists,
                            "assists_confidence": player.assists_confidence,
                            "damage": player.damage,
                            "damage_confidence": player.damage_confidence
                        }
                        for player in team.players
                    ]
                }
                for team in self.teams
            ],
            "image": {
                "width": self.image.width,
                "height": self.image.height
            },
            "processing": {
                "time_ms": self.processing.time_ms,
                "engine": self.processing.engine
            }
        }


class DetectionResult(BaseModel):
    bbox: BoundingBox = Field(description="Relative bounding box (0-1)")
    confidence: float = Field(description="Detection confidence")
    label: str = Field(description="Detection label")
