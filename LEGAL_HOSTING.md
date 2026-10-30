# Legal Documents Hosting Guide

This document provides instructions for hosting the Terms of Service and Privacy Policy for Discord Developer Portal compliance.

## Option 1: GitHub Raw URL (Simplest)

After pushing TERMS.md and PRIVACY.md to your GitHub repository, you can use GitHub's raw content URLs:

**Repository:** https://github.com/Kenzhe17/tournament-draft-bot

### Direct URLs (after pushing to GitHub):

**Terms of Service:**
```
https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/TERMS.md
```

**Privacy Policy:**
```
https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/PRIVACY.md
```

### How to use:
1. Push TERMS.md and PRIVACY.md to GitHub
2. Copy the URLs above
3. Paste them into Discord Developer Portal:
   - Go to your application: https://discord.com/developers/applications/1282609823367823392
   - Navigate to "Information" tab
   - Paste URLs in:
     - "Terms of Service URL"
     - "Privacy Policy URL"

## Option 2: GitHub Pages (Recommended for Better Formatting)

### Step 1: Enable GitHub Pages
1. Go to your repository on GitHub
2. Click "Settings" tab
3. Click "Pages" in left sidebar
4. Under "Source", select "Deploy from a branch"
5. Select "main" branch and "/ (root)" folder
6. Click "Save"

### Step 2: Wait for deployment
- GitHub will deploy your files in 1-2 minutes
- You'll see a URL like: `https://kenzhe17.github.io/tournament-draft-bot/`

### Step 3: Get the URLs
After deployment, your URLs will be:

**Terms of Service:**
```
https://kenzhe17.github.io/tournament-draft-bot/TERMS.md
```

**Privacy Policy:**
```
https://kenzhe17.github.io/tournament-draft-bot/PRIVACY.md
```

## Option 3: Vercel (Custom Domain Support)

If you want a custom domain or better styling:

### Step 1: Install Vercel CLI
```bash
npm install -g vercel
```

### Step 2: Deploy
```bash
cd tournament-draft-bot
vercel
```

### Step 3: Get URLs
Vercel will provide URLs like:
- `https://tournament-draft-bot.vercel.app/TERMS.md`
- `https://tournament-draft-bot.vercel.app/PRIVACY.md`

## Discord Developer Portal Setup

Once you have your URLs:

1. Go to https://discord.com/developers/applications/1282609823367823392
2. Click "Information" in left sidebar
3. Fill in:
   - **Terms of Service URL**: [your TERMS.md URL]
   - **Privacy Policy URL**: [your PRIVACY.md URL]
4. Click "Save Changes"

## Quick Start (Recommended)

For fastest setup, use GitHub Raw URLs:

1. Push TERMS.md and PRIVACY.md to GitHub
2. Use these URLs in Discord Developer Portal:
   - Terms: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/TERMS.md`
   - Privacy: `https://raw.githubusercontent.com/Kenzhe17/tournament-draft-bot/main/PRIVACY.md`

## Notes

- GitHub Raw URLs are permanent and don't require additional setup
- GitHub Pages provides better formatting and caching
- Both options are acceptable for Discord compliance
- Make sure to update the repository name in URLs if you rename your repo
