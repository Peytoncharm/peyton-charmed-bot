# Peyton & Charmed - พี่เจนนี่ LINE Bot

## What This Does
This server sits between LINE and Zoho, forwarding ALL messages to Zoho (keeping your existing system working) while ALSO having Claude AI (พี่เจนนี่) reply to customers on LINE.

## Architecture
```
Customer on LINE
       ↓
LINE Webhook → This Server (Router)
       ↓                    ↓
  Forward to Zoho      Claude (พี่เจนนี่)
  (unchanged)          replies on LINE
```

## Setup

### 1. Deploy to Render.com
1. Push this code to a GitHub repository
2. Go to render.com → New Web Service → Connect your repo
3. Set Environment Variables (see below)
4. Deploy

### 2. Environment Variables
| Variable | Description |
|----------|-------------|
| LINE_CHANNEL_ACCESS_TOKEN | From LINE Developers Console |
| LINE_CHANNEL_SECRET | From LINE Developers Console |
| ANTHROPIC_API_KEY | From console.anthropic.com |
| ZOHO_WEBHOOK_URL | Your current Zoho webhook URL |
| FORWARDING_ONLY | Set to "true" to disable Claude replies (emergency) |

### 3. Update LINE Webhook
1. Go to LINE Developers Console → Your Channel → Messaging API
2. Change Webhook URL to: `https://your-app.onrender.com/callback`
3. Click Verify
4. Ensure "Use webhook" is ON

### 4. Test
1. Send a message on LINE
2. Check that Zoho still receives it
3. Check that พี่เจนนี่ replies

## Files
- `app.py` - Main server (Router + Claude + LINE)
- `system_prompt.py` - พี่เจนนี่'s personality & knowledge (EDIT THIS)
- `requirements.txt` - Python packages
- `render.yaml` - Render.com deployment config

## Safety Features

### Emergency: Disable Claude Replies
If พี่เจนนี่ is behaving unexpectedly, you can instantly disable Claude replies while keeping Zoho forwarding active:

```
POST https://your-app.onrender.com/safety/forwarding-only
```

To re-enable:
```
POST https://your-app.onrender.com/safety/full-mode
```

### Health Check
```
GET https://your-app.onrender.com/health
```

## Updating พี่เจนนี่'s Knowledge
1. Edit `system_prompt.py`
2. Push to GitHub
3. Render auto-deploys

No code changes needed - just edit the Thai text in the system prompt file.
