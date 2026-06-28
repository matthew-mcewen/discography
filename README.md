# Discography

Automated Discord bot for running a weekly screenshot competition. Community members post screenshots in a designated channel; the bot reposts them as voting embeds, counts 🏆 reactions, and announces winners every Monday with a points system.

---

## Requirements

- Python 3.11+
- PostgreSQL 14+

---

## Setup

### 1. Clone and install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a Discord application and bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and create a new application.
2. Under **Bot**, click **Add Bot**. Copy the token.
3. Enable the following **Privileged Gateway Intents**: `SERVER MEMBERS INTENT` and `MESSAGE CONTENT INTENT`.
4. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`. Under Bot Permissions, select at minimum:
   - Read Messages / View Channels
   - Send Messages
   - Manage Messages (to delete invalid posts and clear reactions)
   - Add Reactions
   - Embed Links
   - Manage Messages
   - Read Message History
   - Mention Everyone (for role pings)
   - Pin Messages
5. Use the generated URL to invite the bot to your server.

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```
DISCORD_TOKEN=your_bot_token_here
DATABASE_URL=postgresql://user:password@localhost:5432/discography
```

### 4. Configure the bot

Edit `config.yaml` and replace the `0` placeholders with real IDs:

```yaml
channels:
  submissions: 123456789     # Channel where members post screenshots
  announcements: 123456789   # Channel where weekly results are posted
  bot_commands: 123456789    # Channel where users can run !points etc.
  leaderboard: 123456789     # Channel where the live leaderboard embed lives

roles:
  notification: 123456789    # Role pinged on winner announcements
  dry_run_admin: 123456789   # Role that can use /dryrun-* commands

points:
  name: "Points"             # What your community calls the currency
  emoji: "⭐"               # Unicode emoji or <:customname:ID> for a custom emote
```

To find IDs: enable Developer Mode in Discord settings, then right-click any channel/role and choose **Copy ID**.

### 5. Create the database

```bash
createdb discography
```

The bot runs schema migrations automatically on startup — no manual SQL needed.

### 6. Run the bot

```bash
python main.py
```

---

## How It Works

### Submission channel

- Any message posted in the submissions channel that does **not** contain exactly one image is automatically deleted. The author receives a temporary explanation.
- Valid posts are deleted and reposted as a clean embed by the bot with a 🏆 reaction. This embed is the voting target.
- Only 🏆 reactions are allowed; the bot removes any others in real time.

### Voting

- Every 10 minutes the bot counts the 🏆 reactions on each active submission and updates the database.
- The live leaderboard embed in the leaderboard channel is updated after each count.

### Weekly cycle

| Time | Event |
|------|-------|
| Monday 00:00 UTC | New submission period opens |
| Ongoing | Members post screenshots; votes accumulate |
| Sunday 23:59 UTC | Submission period closes |
| Monday 07:00 UTC | Winners announced in the announcements channel; points awarded |

### Points

- 🥇 1st place: **1000** points
- 🥈 2nd place: **500** points
- 🥉 3rd place: **250** points
- All submitters also receive points equal to their net 🏆 count (total minus the bot's own reaction).

---

## Commands

### Member commands

| Command | Description |
|---------|-------------|
| `/points` | Check your own points balance (ephemeral — only you see the response) |
| `!points` | Same as above, as a prefix command |
| `/leaderboard` | View the current week's top 10 standings (ephemeral) |

### Admin commands

Require the role set as `dry_run_admin` in `config.yaml`.

| Command | Description |
|---------|-------------|
| `/dryrun-count` | Fetch live 🏆 counts for all active submissions and display a preview — no database writes |
| `/dryrun-announce` | Preview exactly what the winner announcement would say and who would receive how many points — nothing is posted and no points are awarded |
| `/dryrun-leaderboard` | Preview the leaderboard embed as it would appear right now — does not update the pinned message |
| `/force-announce` | Immediately run the winner announcement for the most recently ended period. Use this if the bot was offline at Monday 07:00 UTC and missed the scheduled window |

### Submission channel behaviour (automatic, not commands)

| Action | Bot response |
|--------|-------------|
| Post with exactly one image | Original deleted; image reposted as a voting embed with 🏆 |
| Post with no image | Deleted; author notified with a temporary message |
| Post with multiple images | Deleted; author notified with a temporary message |
| Add a non-🏆 reaction to an embed | Reaction removed immediately |

---

## Stale period recovery

If the bot was offline at Monday 07:00 UTC and missed the announcement window, it will log a warning on startup. Use `/dryrun-announce` to preview what would be announced, then `/force-announce` to post results and open the next period.

---

## Development tips

- For faster slash command propagation during development, sync to a specific guild instead of globally. In `main.py`, change `await self.tree.sync()` to `await self.tree.sync(guild=discord.Object(id=YOUR_GUILD_ID))`.
- Logs are written to stdout in the format `timestamp [LEVEL] module: message`.
