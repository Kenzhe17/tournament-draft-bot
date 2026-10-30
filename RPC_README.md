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

### Discord Application Setup (One-time)

**Application ID:** `1282609823367823392`

1. **Create Discord Application:**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Click "New Application"
   - Name it "r!z3 Tournament" (or any name you prefer)
   - Click "Create"

2. **Enable Rich Presence:**
   - In the left sidebar, click "Rich Presence"
   - Click "Enable Rich Presence"
   - Click "Save Changes"

3. **Upload Assets (Images):**
   - In Rich Presence section, click "Art Assets"
   - Upload your tournament logo
   - Name it `logo_rz3` (this matches the code reference)
   - Upload a small image for optional small_image (optional)
   - Click "Save Changes"

4. **Copy Application ID:**
   - On the application's "General Information" page
   - Copy the "APPLICATION ID" (19-digit number)
   - The Application ID for this project is: `1282609823367823392`

### Client Setup

#### Option 1: Using Environment Variable (Recommended)

1. **Create/Edit .env file:**
   ```bash
   # In the same directory as rz3_rpc.py
   DISCORD_CLIENT_ID=123456789012345678
   ```

2. **Install Dependencies:**
   ```bash
   pip install pypresence aiohttp python-dotenv
   ```

3. **Run the Client:**
   ```bash
   python rz3_rpc.py YOUR_DISCORD_ID [SERVER_URL]
   ```

#### Option 2: Editing the Script Directly

1. **Open rz3_rpc.py in a text editor**

2. **Find this line (around line 38):**
   ```python
   DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "YOUR_CLIENT_ID")
   ```

3. **Replace "YOUR_CLIENT_ID" with your actual Application ID:**
   ```python
   DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "123456789012345678")
   ```

4. **Save the file**

5. **Install Dependencies:**
   ```bash
   pip install pypresence aiohttp
   ```

6. **Run the Client:**
   ```bash
   python rz3_rpc.py YOUR_DISCORD_ID [SERVER_URL]
   ```

### Environment Variable Setup Guide

#### Windows (Command Prompt):
```cmd
set DISCORD_CLIENT_ID=123456789012345678
python rz3_rpc.py YOUR_DISCORD_ID
```

#### Windows (PowerShell):
```powershell
$env:DISCORD_CLIENT_ID="123456789012345678"
python rz3_rpc.py YOUR_DISCORD_ID
```

#### Windows (Permanent - System Environment Variables):
1. Press `Win + R`, type `sysdm.cpl`, press Enter
2. Click "Advanced" tab
3. Click "Environment Variables"
4. Under "User variables", click "New"
5. Variable name: `DISCORD_CLIENT_ID`
6. Variable value: `123456789012345678`
7. Click OK on all dialogs
8. Restart Command Prompt/PowerShell

#### Linux/macOS (Temporary):
```bash
export DISCORD_CLIENT_ID=123456789012345678
python rz3_rpc.py YOUR_DISCORD_ID
```

#### Linux/macOS (Permanent - .bashrc or .zshrc):
1. Open `~/.bashrc` (for Bash) or `~/.zshrc` (for Zsh)
2. Add this line at the end:
   ```bash
   export DISCORD_CLIENT_ID=123456789012345678
   ```
3. Save the file
4. Run `source ~/.bashrc` or `source ~/.zshrc`
5. Or restart your terminal

#### Using .env file (Recommended for project):
1. Create a file named `.env` in the same directory as `rz3_rpc.py`
2. Add this line:
   ```
   DISCORD_CLIENT_ID=123456789012345678
   ```
3. The script will automatically read this file

### Running the Client

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

### Regular Tournament Scenarios

**SETUP Phase (Registration):**
- **Details:** "Регистрация"
- **State:** "Phase: SETUP"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [registered/total] (e.g., [8/8], [16/16], [32/32])
- **Start Time:** None (no timer in lobby)

**DRAFT Phase (Team Selection):**
- **Details:** "Драфт команд"
- **State:** "Phase: DRAFT"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [registered/total]
- **Start Time:** None (no timer in lobby)

**QUALIFIERS Phase:**
- **Details:** "Qualifiers"
- **State:** "Team 1" / "Team 2" / "Team 3" / "Team 4"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (team size)
- **Start Time:** Match duration timer (starts when phase begins)

**SEMIFINALS Phase:**
- **Details:** "Semifinals"
- **State:** "Team 1" / "Team 2" / "Team 3" / "Team 4"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (team size)
- **Start Time:** Match duration timer

**FINALS Phase:**
- **Details:** "Finals"
- **State:** "Team 1" / "Team 2" / "Team 3" / "Team 4"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (team size)
- **Start Time:** Match duration timer

**COMPLETE Phase:**
- **Status:** Cleared (no Rich Presence)

### BR Tournament Scenarios

**REGISTRATION Phase:**
- **Details:** "BR Tournament Registration"
- **State:** "Players: X/48" (e.g., "Players: 24/48")
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [X/48]
- **Start Time:** None (no timer in lobby)

**BERMUDA Phase (Map 1):**
- **Details:** "BR Cup - Bermuda"
- **State:** "Team: Team Name" (e.g., "Team: Team Alpha")
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (squad size)
- **Start Time:** Match duration timer

**BERMUDA Phase (Map 4, if maps=4):**
- **Details:** "BR Cup - Bermuda"
- **State:** "Team: Team Name"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (squad size)
- **Start Time:** Match duration timer

**PURGATORY Phase:**
- **Details:** "BR Cup - Purgatory"
- **State:** "Team: Team Name"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (squad size)
- **Start Time:** Match duration timer

**KALAHARI Phase:**
- **Details:** "BR Cup - Kalahari"
- **State:** "Team: Team Name"
- **Large Image:** r!z3 logo
- **Large Text:** "r!z3 Tournament System"
- **Party Size:** [4/4] (squad size)
- **Start Time:** Match duration timer

**RESULTS Phase:**
- **Status:** Cleared (no Rich Presence)

**COMPLETE Phase:**
- **Status:** Cleared (no Rich Presence)

### No Active Tournament

**Player not in any tournament:**
- **Status:** Cleared (no Rich Presence)
- Reason: Player is not registered in any active tournament

### Summary Table

#### 1. Обычные турниры (Tournament Draft)

| Фаза турнира | Статус в БД | Details | State | Кнопка / Пати | Таймер |
|-------------|-------------|---------|-------|---------------|---------|
| Регистрация / Настройка | IN_LOBBY | Регистрация | Phase: SETUP | [X/всего] | - |
| Драфт команд | IN_LOBBY | Драфт команд | Phase: DRAFT | [X/всего] | - |
| Квалификации | IN_GAME | Qualifiers | Team 1 (название команды) | [4/4] | ✓ |
| Полуфинал | IN_GAME | Semifinals | Team 1 (название команды) | [4/4] | ✓ |
| Финал | IN_GAME | Finals | Team 1 (название команды) | [4/4] | ✓ |
| Завершён | none | (очищается) | - | - | - |

#### 2. Battle Royale (BR) турниры

| Карта / Фаза | Статус в БД | Details | State | Кнопка / Пати | Таймер |
|--------------|-------------|---------|-------|---------------|---------|
| Регистрация | IN_LOBBY | BR Tournament Registration | Players: X/48 | [X/48] | - |
| Карта: Бермуды | IN_GAME | BR Cup - Bermuda | Team: [Название] | [4/4] | ✓ |
| Карта: Чистилище | IN_GAME | BR Cup - Purgatory | Team: [Название] | [4/4] | ✓ |
| Карта: Калахари | IN_GAME | BR Cup - Kalahari | Team: [Название] | [4/4] | ✓ |
| Подсчёт результатов / Конец | none | (очищается) | - | - | - |

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
