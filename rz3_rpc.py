"""
r!z3 Tournament Discord Rich Presence Client

This script runs on player's PC during tournaments to show their current status
in Discord profile via Discord Rich Presence.

Usage:
    python rz3_rpc.py YOUR_DISCORD_ID [SERVER_URL]

Example:
    python rz3_rpc.py 123456789012345678
    python rz3_rpc.py 123456789012345678 http://localhost:8080
"""

import asyncio
import os
import time
import sys
import logging
from typing import Optional

try:
    from pypresence import Presence
    from pypresence.exceptions import DiscordNotFound, InvalidID
except ImportError:
    print("pypresence not installed. Install with: pip install pypresence")
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    print("aiohttp not installed. Install with: pip install aiohttp")
    sys.exit(1)


# Discord Application ID for r!z3 Tournament
# Get this from https://discord.com/developers/applications
# Create an application, enable Rich Presence, and copy the Application ID
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "YOUR_CLIENT_ID")

# Default server URL (can be overridden with command line argument)
DEFAULT_SERVER_URL = "http://localhost:8080"

# Update interval in seconds
UPDATE_INTERVAL = 15

# Rich Presence configuration
RPC_CONFIG = {
    "large_image": "logo_rz3",
    "large_text": "r!z3 Tournament System",
    "small_image": None,
    "small_text": None,
}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class RPCClient:
    """Discord Rich Presence client for r!z3 Tournament System."""

    def __init__(self, discord_id: int, server_url: str = DEFAULT_SERVER_URL):
        self.discord_id = discord_id
        self.server_url = server_url.rstrip("/")
        self.rpc: Optional[Presence] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.current_status = "none"
        self.running = False

    async def start(self):
        """Start the RPC client."""
        logger.info(f"Starting RPC client for Discord ID: {self.discord_id}")
        logger.info(f"Server URL: {self.server_url}")

        # Start aiohttp session
        self.session = aiohttp.ClientSession()

        # Connect to Discord RPC
        try:
            self.rpc = Presence(DISCORD_CLIENT_ID)
            await self.rpc.connect()
            logger.info("Connected to Discord RPC")
        except DiscordNotFound:
            logger.error("Discord not found. Make sure Discord is running.")
            await self.cleanup()
            sys.exit(1)
        except InvalidID:
            logger.error(f"Invalid Discord Client ID: {DISCORD_CLIENT_ID}")
            await self.cleanup()
            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to connect to Discord RPC: {e}")
            await self.cleanup()
            sys.exit(1)

        self.running = True

    async def cleanup(self):
        """Cleanup resources."""
        self.running = False

        if self.rpc:
            try:
                await self.rpc.close()
                logger.info("RPC connection closed")
            except Exception as e:
                logger.error(f"Error closing RPC: {e}")

        if self.session:
            try:
                await self.session.close()
                logger.info("HTTP session closed")
            except Exception as e:
                logger.error(f"Error closing session: {e}")

    async def fetch_status(self) -> Optional[dict]:
        """Fetch player status from server."""
        try:
            url = f"{self.server_url}/status/{self.discord_id}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.warning(f"Server returned status {response.status}")
                    return None
        except asyncio.TimeoutError:
            logger.warning("Timeout fetching status from server")
            return None
        except aiohttp.ClientError as e:
            logger.warning(f"HTTP error fetching status: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching status: {e}")
            return None

    async def update_presence(self, status_data: dict):
        """Update Discord Rich Presence."""
        try:
            status = status_data.get("status", "none")

            # Clear presence if not in game/lobby
            if status == "none":
                if self.current_status != "none":
                    await self.rpc.clear()
                    self.current_status = "none"
                    logger.info("Cleared Discord presence")
                return

            # Build presence payload
            presence_data = {
                "details": status_data.get("details", "Playing r!z3 Tournament"),
                "state": status_data.get("state", ""),
                "large_image": status_data.get("large_image", RPC_CONFIG["large_image"]),
                "large_text": status_data.get("large_text", RPC_CONFIG["large_text"]),
            }

            # Add party size if available
            party_size = status_data.get("party_size")
            if party_size and isinstance(party_size, list) and len(party_size) == 2:
                presence_data["party_size"] = party_size

            # Add start time if available
            start_time = status_data.get("start_time")
            if start_time:
                presence_data["start"] = start_time

            await self.rpc.update(**presence_data)
            self.current_status = status
            logger.info(f"Updated presence: {presence_data}")

        except Exception as e:
            logger.error(f"Error updating presence: {e}")

    async def run_loop(self):
        """Main update loop."""
        logger.info("Starting update loop (15s interval)")

        while self.running:
            try:
                # Fetch status from server
                status_data = await self.fetch_status()

                if status_data:
                    await self.update_presence(status_data)
                else:
                    # Server error - clear presence
                    if self.current_status != "none":
                        await self.rpc.clear()
                        self.current_status = "none"
                        logger.info("Cleared presence due to server error")

            except Exception as e:
                logger.error(f"Error in update loop: {e}")

            # Wait for next update
            await asyncio.sleep(UPDATE_INTERVAL)

    async def run(self):
        """Run the RPC client."""
        try:
            await self.start()
            await self.run_loop()
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
        finally:
            await self.cleanup()


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python rz3_rpc.py YOUR_DISCORD_ID [SERVER_URL]")
        print("Example: python rz3_rpc.py 123456789012345678")
        print("Example: python rz3_rpc.py 123456789012345678 http://localhost:8080")
        sys.exit(1)

    try:
        discord_id = int(sys.argv[1])
    except ValueError:
        print("Error: Discord ID must be a number")
        sys.exit(1)

    server_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SERVER_URL

    print(f"r!z3 Tournament RPC Client")
    print(f"Discord ID: {discord_id}")
    print(f"Server URL: {server_url}")
    print(f"Update interval: {UPDATE_INTERVAL}s")
    print("Press Ctrl+C to stop")
    print("-" * 50)

    # Run async client
    client = RPCClient(discord_id, server_url)
    asyncio.run(client.run())


if __name__ == "__main__":
    main()
