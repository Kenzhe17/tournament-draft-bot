"""Test script for OCR calibration."""

import cv2
import sys
from services.image_analyzer import image_analyzer

# Test with one of the screenshots
screenshot_path = r"C:\Users\Nota\Projects\tournament-draft-bot\screens\IMG_1810.png"

# Expected players (example - replace with actual player names from your tournament)
expected_players = ["Player1", "Player2", "Player3", "Player4", "Player5", "Player6", "Player7", "Player8"]

print(f"Testing OCR with: {screenshot_path}")
print(f"Expected players: {expected_players}")
print("-" * 50)

result = image_analyzer.analyze_screenshot(screenshot_path, expected_players)

if result:
    print(f"✅ OCR Success!")
    print(f"Score: {result.score}")
    print(f"Winner: {result.winner_team}")
    print(f"Loser: {result.loser_team}")
    print(f"\nTeam 1 Players:")
    for player in result.team1_players:
        print(f"  {player.nickname} - K:{player.kills} D:{player.deaths} A:{player.assists} DMG:{player.damage} (Conf: {player.confidence:.1f}%)")
    print(f"\nTeam 2 Players:")
    for player in result.team2_players:
        print(f"  {player.nickname} - K:{player.kills} D:{player.deaths} A:{player.assists} DMG:{player.damage} (Conf: {player.confidence:.1f}%)")
else:
    print("❌ OCR Failed - check image and coordinates")
