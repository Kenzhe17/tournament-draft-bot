"""Calibration script for OCR region detection."""

import cv2
import numpy as np

# Load the screenshot
screenshot_path = r"C:\Users\Nota\Projects\tournament-draft-bot\screens\IMG_1810.png"
image = cv2.imread(screenshot_path)

if image is None:
    print(f"Failed to load image: {screenshot_path}")
    exit(1)

height, width = image.shape[:2]
print(f"Image dimensions: {width}x{height}")

# Create a copy for visualization
vis_image = image.copy()

# Current region coordinates
score_y_start = int(height * 0.1)
score_y_end = int(height * 0.2)
score_x_start = int(width * 0.3)
score_x_end = int(width * 0.7)

team1_y_start = int(height * 0.25)
team1_y_end = int(height * 0.85)
team1_x_start = int(width * 0.05)
team1_x_end = int(width * 0.45)

team2_y_start = int(height * 0.25)
team2_y_end = int(height * 0.85)
team2_x_start = int(width * 0.55)
team2_x_end = int(width * 0.95)

# Draw regions on the image
# Score region - Red
cv2.rectangle(vis_image, (score_x_start, score_y_start), (score_x_end, score_y_end), (0, 0, 255), 2)
cv2.putText(vis_image, "Score", (score_x_start, score_y_start - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

# Team 1 region - Green
cv2.rectangle(vis_image, (team1_x_start, team1_y_start), (team1_x_end, team1_y_end), (0, 255, 0), 2)
cv2.putText(vis_image, "Team 1", (team1_x_start, team1_y_start - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

# Team 2 region - Blue
cv2.rectangle(vis_image, (team2_x_start, team2_y_start), (team2_x_end, team2_y_end), (255, 0, 0), 2)
cv2.putText(vis_image, "Team 2", (team2_x_start, team2_y_start - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)

# Save the visualization
output_path = r"C:\Users\Nota\Projects\tournament-draft-bot\screens\calibration_result.png"
cv2.imwrite(output_path, vis_image)

print(f"Calibration visualization saved to: {output_path}")
print("\nCurrent region coordinates:")
print(f"Score: x={score_x_start}-{score_x_end}, y={score_y_start}-{score_y_end}")
print(f"Team 1: x={team1_x_start}-{team1_x_end}, y={team1_y_start}-{team1_y_end}")
print(f"Team 2: x={team2_x_start}-{team2_x_end}, y={team2_y_start}-{team2_y_end}")

# Extract and save individual regions for inspection
score_region = image[score_y_start:score_y_end, score_x_start:score_x_end]
cv2.imwrite(r"C:\Users\Nota\Projects\tournament-draft-bot\screens\region_score.png", score_region)

team1_region = image[team1_y_start:team1_y_end, team1_x_start:team1_x_end]
cv2.imwrite(r"C:\Users\Nota\Projects\tournament-draft-bot\screens\region_team1.png", team1_region)

team2_region = image[team2_y_start:team2_y_end, team2_x_start:team2_x_end]
cv2.imwrite(r"C:\Users\Nota\Projects\tournament-draft-bot\screens\region_team2.png", team2_region)

print("\nIndividual regions saved to screens folder")
