from typing import List
from .models import ParseResult, Team, PlayerStats


class ResultValidator:
    """Validate parsing results for consistency and correctness."""
    
    @staticmethod
    def validate(result: ParseResult) -> bool:
        """
        Validate the complete parse result.
        
        Args:
            result: ParseResult to validate
            
        Returns:
            True if valid, False otherwise
        """
        if not result.success:
            return False
        
        # Check teams
        if len(result.teams) != 2:
            return False
        
        # Check each team
        for team in result.teams:
            if not ResultValidator._validate_team(team):
                return False
        
        # Check score
        if result.match.score:
            if result.match.score.team1 < 0 or result.match.score.team2 < 0:
                return False
            if result.match.score.team1 > 20 or result.match.score.team2 > 20:
                return False
        
        # Check winner
        if result.match.winner not in [0, 1, 2]:
            return False
        
        return True
    
    @staticmethod
    def _validate_team(team: Team) -> bool:
        """Validate a single team."""
        if team.id not in [1, 2]:
            return False
        
        if len(team.players) != 4:
            return False
        
        for player in team.players:
            if not ResultValidator._validate_player(player):
                return False
        
        return True
    
    @staticmethod
    def _validate_player(player: PlayerStats) -> bool:
        """Validate a single player."""
        if player.position not in [1, 2, 3, 4]:
            return False
        
        if not player.nickname or len(player.nickname) < 2:
            return False
        
        if player.kills < 0 or player.kills > 50:
            return False
        
        if player.deaths < 0 or player.deaths > 50:
            return False
        
        if player.assists < 0 or player.assists > 50:
            return False
        
        if player.damage < 0 or player.damage > 20000:
            return False
        
        if player.nickname_confidence < 0 or player.nickname_confidence > 1:
            return False
        
        if player.kills_confidence < 0 or player.kills_confidence > 1:
            return False
        
        if player.deaths_confidence < 0 or player.deaths_confidence > 1:
            return False
        
        if player.assists_confidence < 0 or player.assists_confidence > 1:
            return False
        
        if player.damage_confidence < 0 or player.damage_confidence > 1:
            return False
        
        return True
    
    @staticmethod
    def get_validation_errors(result: ParseResult) -> List[str]:
        """
        Get detailed validation errors.
        
        Args:
            result: ParseResult to validate
            
        Returns:
            List of error messages
        """
        errors = []
        
        if not result.success:
            errors.append(f"Parse failed: {result.error}")
            return errors
        
        if len(result.teams) != 2:
            errors.append(f"Expected 2 teams, got {len(result.teams)}")
        
        for team in result.teams:
            if team.id not in [1, 2]:
                errors.append(f"Invalid team ID: {team.id}")
            
            if len(team.players) != 4:
                errors.append(f"Team {team.id} has {len(team.players)} players, expected 4")
            
            for player in team.players:
                if not player.nickname:
                    errors.append(f"Player {player.position} in team {team.id} has empty nickname")
                
                if player.kills < 0 or player.kills > 50:
                    errors.append(f"Player {player.position} in team {team.id} has invalid kills: {player.kills}")
                
                if player.damage < 0 or player.damage > 20000:
                    errors.append(f"Player {player.position} in team {team.id} has invalid damage: {player.damage}")
        
        if result.match.score:
            if result.match.score.team1 < 0 or result.match.score.team1 > 20:
                errors.append(f"Invalid team1 score: {result.match.score.team1}")
            
            if result.match.score.team2 < 0 or result.match.score.team2 > 20:
                errors.append(f"Invalid team2 score: {result.match.score.team2}")
        
        return errors
