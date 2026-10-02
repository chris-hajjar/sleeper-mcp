# sleeper-mcp

A Model Context Protocol (MCP) server for Sleeper Fantasy Football. Ask Claude natural-language questions about your team, league, and players — get real answers backed by live Sleeper data.

## What you can ask

- *"How's my team looking this week?"*
- *"Who should I start — Jaylen Wright or Rhamondre Stevenson?"*
- *"Analyze this trade: I give Justin Jefferson, I get CeeDee Lamb"*
- *"Who are the best waiver pickups at RB right now?"*
- *"What's my schedule strength for the rest of the season?"*
- *"Give me a recap of last week's matchups"*
- *"Which teams are on the playoff bubble?"*

## Tools

| Tool | Description |
|---|---|
| `set_context` | Configure your username and league (run once) |
| `get_context_info` | Show current user/league |
| `get_my_team` | Your full roster: starters, bench, IR, bye weeks, injuries |
| `get_my_season` | Season summary: week-by-week results, record, playoff status |
| `get_matchup` | Your matchup for any week with scores and starters |
| `get_standings` | Full league standings with points for/against |
| `get_playoff_picture` | Who's in, bubble teams, games back |
| `get_week_recap` | All matchup results for any given week |
| `get_free_agents` | Available players by position |
| `get_trending_players` | Hot adds and drops across all Sleeper leagues |
| `search_player` | Look up any player: status, injury, depth chart |
| `get_transactions` | Recent adds, drops, trades, and waiver claims |
| `analyze_trade` | Trade analysis: positional value, age, rank comparison |
| `get_waiver_targets` | Best pickups based on your roster's needs |
| `get_schedule_strength` | Remaining schedule difficulty for every team |
| `get_draft_recap` | Draft history and outstanding traded picks |

## Local setup

### Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Install

```bash
git clone https://github.com/chris-hajjar/sleeper-mcp
cd sleeper-mcp
uv venv .venv
source .venv/bin/activate
uv pip install -e .
```

### Configure Claude Desktop

Add this to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on Mac):

```json
{
  "mcpServers": {
    "sleeper": {
      "command": "/path/to/sleeper-mcp/.venv/bin/python",
      "args": ["-m", "sleeper_mcp.server"],
      "env": {
        "SLEEPER_USERNAME": "your_sleeper_username",
        "SLEEPER_LEAGUE_ID": "your_league_id"
      }
    }
  }
}
```

Replace `/path/to/sleeper-mcp` with the actual path (e.g. `/Users/yourname/Desktop/sleeper-mcp`).

Your league ID is in the URL when you visit your league: `sleeper.com/leagues/<LEAGUE_ID>/team`

### First run

After restarting Claude Desktop, ask:

```
set_context with my sleeper username
```

Your username and league are saved locally — you won't need to repeat this.

## Remote setup (Claude mobile)

To use this on the Claude mobile app, deploy it as a remote server.

### Railway (recommended)

1. Push this repo to GitHub
2. Create a new project at [railway.app](https://railway.app)
3. Connect your GitHub repo
4. Set environment variables in Railway:
   - `SLEEPER_USERNAME` — your Sleeper username
   - `SLEEPER_LEAGUE_ID` — your league ID
5. Railway auto-deploys on push

Then add the Railway URL as a remote MCP in Claude's settings:
```
https://your-app.railway.app/mcp
```

The server supports both stdio (local) and Streamable HTTP (remote) via FastMCP's built-in transport handling.

## Auth and write operations

All read tools work with no authentication — the Sleeper public API requires no keys or tokens.

For write operations (setting lineups, submitting waiver claims, proposing trades), you need your Sleeper session token:

1. Open Sleeper in Chrome, log in
2. Open DevTools → Console
3. Run: `localStorage.getItem('token')`
4. Copy the result and add it as `SLEEPER_TOKEN` in your environment

Tokens last approximately one year. **Never commit your token to git.**

## Data sources

- Sleeper public API (`api.sleeper.app/v1`) — leagues, rosters, matchups, transactions, players, drafts
- Sleeper stats API (`api.sleeper.app/v1/stats`) — weekly and season-long player stats (Sportradar data)
- Sleeper projections API (`api.sleeper.app/v1/projections`) — weekly player projections
- Sleeper trending API — add/drop volume across all leagues

The full player database (~5MB) is cached locally for 24 hours.

## Project structure

```
sleeper_mcp/
├── server.py     # FastMCP app and all 16 tool definitions
├── client.py     # Async HTTP client for Sleeper API
├── cache.py      # Player DB cache and context storage
└── __init__.py
test_live.py      # Integration tests against real API
```
