# r!z3 Dashboard

Next.js dashboard for the r!z3 Discord tournament bot.

## Environment Variables

Required environment variables for Vercel deployment:

```env
DATABASE_URL=postgresql://user:password@host:port/database
DISCORD_CLIENT_ID=your_discord_client_id
DISCORD_CLIENT_SECRET=your_discord_client_secret
NEXTAUTH_SECRET=your_nextauth_secret
NEXTAUTH_URL=https://your-dashboard.vercel.app
```

## Setup

1. Install dependencies:
```bash
npm install
```

2. Run development server:
```bash
npm run dev
```

3. Build for production:
```bash
npm run build
npm start
```

## API Endpoints

- `GET /api/leaderboard?guild_id={guild_id}&limit=10` - Get leaderboard
- `GET /api/pet?user_id={user_id}` - Get user's pet
- `POST /api/pet/feed` - Feed pet
- `GET /api/user/profile?user_id={user_id}&guild_id={guild_id}` - Get user profile

## Deployment

Deploy to Vercel:
1. Connect your GitHub repository
2. Import the `dashboard` folder
3. Add environment variables
4. Deploy
