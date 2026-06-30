import sys
import time
from pathlib import Path
from typing import List

# Add parser to path
sys.path.insert(0, str(Path(__file__).parent))

from parser.pipeline import ParserPipeline


def get_image_files(screens_dir: str) -> List[Path]:
    """Get all image files from screens directory."""
    screens_path = Path(screens_dir)
    supported_extensions = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}
    
    image_files = []
    for file_path in screens_path.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in supported_extensions:
            image_files.append(file_path)
    
    return sorted(image_files)


def run_batch_test(screens_dir: str = "screens", 
                  output_dir: str = "output", 
                  debug_dir: str = "debug"):
    """
    Run batch processing on all screenshots.
    
    Args:
        screens_dir: Directory containing screenshots
        output_dir: Directory for JSON outputs
        debug_dir: Directory for debug visualizations
    """
    # Create directories
    Path(output_dir).mkdir(exist_ok=True)
    Path(debug_dir).mkdir(exist_ok=True)
    
    # Get image files
    image_files = get_image_files(screens_dir)
    
    if not image_files:
        print(f"No images found in {screens_dir}")
        return
    
    print(f"Found {len(image_files)} images to process")
    print("=" * 50)
    
    # Initialize pipeline
    pipeline = ParserPipeline(debug_dir=debug_dir)
    
    # Statistics
    total = len(image_files)
    success = 0
    failed = 0
    times = []
    
    # Process each image
    for i, image_path in enumerate(image_files, 1):
        print(f"[{i}/{total}] Processing: {image_path.name}")
        
        # Create debug subdirectory for this image
        image_debug_dir = Path(debug_dir) / image_path.stem
        pipeline.debug_dir = str(image_debug_dir)
        pipeline.debug = type(pipeline.debug)(image_debug_dir) if pipeline.debug else None
        
        start_time = time.time()
        result = pipeline.parse(str(image_path))
        elapsed = time.time() - start_time
        times.append(elapsed * 1000)  # Convert to ms
        
        # Save result
        output_path = Path(output_dir) / f"{image_path.stem}.json"
        pipeline.save_result(result, str(output_path))
        
        # Update statistics
        if result.success:
            success += 1
            print(f"  ✓ Success ({elapsed*1000:.0f}ms)")
        else:
            failed += 1
            print(f"  ✗ Failed: {result.error} ({elapsed*1000:.0f}ms)")
        
        print()
    
    # Print summary
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"Processed: {total}")
    print(f"Success: {success}")
    print(f"Failed: {failed}")
    
    if times:
        avg_time = sum(times) / len(times)
        print(f"Average time: {avg_time:.0f}ms")
        print(f"Min time: {min(times):.0f}ms")
        print(f"Max time: {max(times):.0f}ms")
    
    print(f"\nOutput saved to: {output_dir}/")
    print(f"Debug images saved to: {debug_dir}/")


def run_single_test(image_path: str, 
                    output_path: str = None, 
                    debug_dir: str = "debug"):
    """
    Run processing on a single image.
    
    Args:
        image_path: Path to the screenshot
        output_path: Path for JSON output (optional)
        debug_dir: Directory for debug visualizations
    """
    # Create debug directory
    image_debug_dir = Path(debug_dir) / Path(image_path).stem
    image_debug_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize pipeline
    pipeline = ParserPipeline(debug_dir=str(image_debug_dir))
    
    print(f"Processing: {image_path}")
    start_time = time.time()
    result = pipeline.parse(image_path)
    elapsed = time.time() - start_time
    
    # Save result
    if output_path is None:
        output_path = f"output/{Path(image_path).stem}.json"
    
    Path(output_path).parent.mkdir(exist_ok=True)
    pipeline.save_result(result, output_path)
    
    # Print result
    print(f"Time: {elapsed*1000:.0f}ms")
    
    if result.success:
        print("✓ Success")
        print(f"Winner: Team {result.match.winner}")
        if result.match.score:
            print(f"Score: {result.match.score.team1} : {result.match.score.team2}")
        print(f"\nTeam 1: {result.teams[0].name}")
        for player in result.teams[0].players:
            print(f"  {player.position}. {player.nickname} - K:{player.kills} D:{player.deaths} A:{player.assists} DMG:{player.damage}")
        print(f"\nTeam 2: {result.teams[1].name}")
        for player in result.teams[1].players:
            print(f"  {player.position}. {player.nickname} - K:{player.kills} D:{player.deaths} A:{player.assists} DMG:{player.damage}")
    else:
        print(f"✗ Failed: {result.error}")
    
    print(f"\nOutput saved to: {output_path}")
    print(f"Debug images saved to: {image_debug_dir}/")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Free Fire Match Statistics Parser")
    parser.add_argument("--image", type=str, help="Process single image")
    parser.add_argument("--screens", type=str, default="screens", help="Screens directory")
    parser.add_argument("--output", type=str, default="output", help="Output directory")
    parser.add_argument("--debug", type=str, default="debug", help="Debug directory")
    
    args = parser.parse_args()
    
    if args.image:
        run_single_test(args.image, args.output, args.debug)
    else:
        run_batch_test(args.screens, args.output, args.debug)
