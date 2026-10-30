# r!z3 Tournament Discord Rich Presence

## Overview

This module allows tournament participants to display their current tournament status in their Discord profile via Discord Rich Presence (RPC).

## Components

### 1. Server Side (Bot)

The bot includes an HTTP server (`rpc_server.py`) that provides an API endpoint for checking player status.

**Endpoint:**
- `GET /status/{discord_id}` - Returns the player's current tournament status

**Response Format:**
```json
{
  "status": "IN_GAME | IN_LOBBY | none",
  "details": "Tournament name or stage",
  "state": "Player role or lobby status",
  "party_size": [current, max],
  "large_image": "logo_rz3",
  "large_text": "r!z3 Tournament System",
  "start_time": 1234567890  // optional
}
```

**Status Types:**
- `IN_GAME` - Player is in an active match (Qualifiers, Semifinals, Finals, or BR match)
- `IN_LOBBY` - Player is in registration or draft phase
- `none` - Player is not in any active tournament

### 2. Client Side (Player's PC)

The client script (`rz3_rpc.py`) runs on the player's computer during tournaments and communicates with the bot's HTTP server.

## Setup

### Server Setup (Already Integrated)

The RPC server is automatically started with the bot in `bot.py`. No additional configuration needed.

### Client Setup

1. **Install Dependencies:**
   ```bash
   pip install pypresence aiohttp
   ```

2. **Configure Discord Application:**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Create a new application or use existing one
   - Enable Rich Presence
   - Upload assets (logo images) in the Rich Presence Assets section
   - Note the Application ID

3. **Update Client ID:**
   - Edit `rz3_rpc.py`
   - Replace `YOUR_CLIENT_ID` with your actual Discord Application ID

4. **Run the Client:**
   ```bash
   python rz3_rpc.py YOUR_DISCORD_ID [SERVER_URL]
   ```

   **Examples:**
   ```bash
   # Local development
   python rz3_rpc.py 123456789012345678

   # Remote server
   python rz3_rpc.py 123456789012345678 http://your-server.com:8080
   ```

## Discord Rich Presence Display

When active, the player's Discord profile will show:

**In Lobby:**
- **Details:** "Tournament Setup" or "BR Tournament Registration"
- **State:** "Phase: SETUP" or "Players: 24/48"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [current/total]

**In Game:**
- **Details:** "Qualifiers" or "BR Cup - Bermuda"
- **State:** "Team 1" or "Team: Team Name"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] or [24/48]
- **Start Time:** Match duration timer

## Features

- **Auto-Update:** Status updates every 15 seconds
- **Error Handling:** Gracefully handles Discord not running, server errors, etc.
- **Status Detection:** Automatically detects regular tournaments and BR tournaments
- **Clear on Inactivity:** Clears presence when not in an active tournament
- **Tournament Details:** Shows specific tournament phase and team information

## Troubleshooting

### Discord not found
- Make sure Discord application is running
- Check that Discord is logged in

### Server connection error
- Verify the server URL is correct
- Check that the bot is running and RPC server is accessible
- Check firewall settings

### Presence not updating
- Check bot logs for RPC server errors
- Verify player is registered in a tournament
- Check that tournament is in active phase (not COMPLETE)

### Invalid Client ID
- Verify the Discord Application ID is correct
- Ensure Rich Presence is enabled in Discord Developer Portal

## Deployment Notes

For Railway/cloud deployment:

1. The RPC server runs on port 8080 by default
2. Make sure port 8080 is exposed in your deployment configuration
3. Players should use the public URL of your Railway instance
4. Example: `python rz3_rpc.py 123456789012345678 https://your-app.railway.app`

## Security

- The RPC server only provides read-only status information
- No authentication required (tournament status is public to participants)
- Consider adding authentication if deploying publicly

## Future Enhancements

- Add authentication for security
- Support for custom status messages
- Match timer integration
- Team logo display
- Map-specific images
