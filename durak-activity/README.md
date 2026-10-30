# Durak Discord Activity

Discord Embedded Application for playing the card game "Durak" (Дурак) directly in voice channels.

## Features

- **Game Modes**: Podkidnoy (Подкидной) and Perevodnoy (Переводной) Durak
- **Player Support**: 2-6 players
- **Real-time Multiplayer**: Using Socket.IO for real-time game state synchronization
- **Discord Integration**: Uses Discord Embedded App SDK for avatars, usernames, and authentication
- **Modern UI**: React + Vite with Discord-style dark theme
- **SVG Card Graphics**: Clean vector card graphics

## Tech Stack

### Frontend
- React 18
- Vite
- @discord/embedded-app-sdk
- Socket.IO Client

### Backend
- Node.js
- Express
- Socket.IO
- CORS

## Setup

### Prerequisites
- Node.js 18+
- Discord Developer Portal access
- Discord Application ID: `1782609823367873392`

### Frontend Setup

```bash
cd durak-activity
npm install
npm run dev
```

The frontend will run on `http://localhost:3000`

### Backend Setup

```bash
cd durak-activity/server
npm install
npm start
```

The backend will run on `http://localhost:3001`

### Building for Production

```bash
cd durak-activity
npm run build
```

The built files will be in the `dist/` directory.

## Deployment

### Option 1: Vercel (Recommended for Frontend)

1. Install Vercel CLI:
```bash
npm install -g vercel
```

2. Deploy:
```bash
cd durak-activity
vercel
```

3. Get the URL and use it in Discord Developer Portal.

### Option 2: Netlify

1. Build the project:
```bash
npm run build
```

2. Deploy the `dist/` folder to Netlify.

### Option 3: Railway (Full Stack)

1. Create a new project on Railway
2. Connect your GitHub repository
3. Set environment variables:
   - `PORT`: 3001
4. Railway will automatically deploy both frontend and backend.

## Discord Developer Portal Setup

1. Go to your application: https://discord.com/developers/applications/1782609823367873392

2. Navigate to "URL Mappings" in the left sidebar

3. Add your deployed URL:
   - Development: `http://localhost:3000` (for local testing)
   - Production: `https://your-deployed-url.vercel.app`

4. Navigate to "Embedded App"
   - Enable "Embedded App"
   - Configure permissions: `identify`, `guilds`

5. Navigate to "Information"
   - Add Terms of Service URL: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/TERMS.md`
   - Add Privacy Policy URL: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/PRIVACY.md`

## Bot Integration

The bot will have a slash command to launch the activity:

```python
@bot.tree.command(name="durak", description="Запустить игру Дурак в голосовом канале")
async def durak(interaction: discord.Interaction):
    # Create invite for embedded application
    invite = await interaction.channel.create_invite(
        target_type=discord.InviteTarget.embedded_application,
        target_application_id=1782609823367873392
    )
    await interaction.response.send_message(f"🃏 Запускаю Дурак! {invite.url}")
```

## Game Rules

### Podkidnoy Durak (Подкидной)
- Classic version where you can add cards to the attack
- Defender must beat all cards or take them
- Game ends when deck is empty and one player runs out of cards

### Perevodnoy Durak (Переводной)
- Defender can transfer attack to next player
- Transfer only allowed if matching rank card is in hand
- Strategic gameplay with passing mechanics

## Current Status

- ✅ Basic React setup
- ✅ Discord SDK integration
- ✅ Card component with SVG styling
- ✅ Lobby screen
- ✅ Game table UI
- ✅ Basic game logic (deck, card comparison)
- ✅ Socket.IO server structure
- ⏳ Full multiplayer game logic
- ⏳ Real-time synchronization
- ⏳ Win/lose conditions
- ⏳ Bot integration command

## TODO

- [ ] Complete multiplayer game logic
- [ ] Implement turn-based system
- [ ] Add game timer
- [ ] Implement trump card mechanics
- [ ] Add card animations
- [ ] Add sound effects
- [ ] Implement translation mode logic
- [ ] Add spectator mode
- [ ] Add game history
- [ ] Integrate with bot economy system

## License

MIT License
