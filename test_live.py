"""
Live integration test — hits real Sleeper API with Chris's league.
Run: python test_live.py
"""
import asyncio
import sys
import os

# Inject known good context so tests don't need env vars
os.environ.setdefault("SLEEPER_USERNAME", "chriscr ingle19")
os.environ.setdefault("SLEEPER_LEAGUE_ID", "1389328983354531840")

sys.path.insert(0, ".")
from sleeper_mcp.client import SleeperClient
from sleeper_mcp import cache

USER_ID = "1115531118549311488"
LEAGUE_ID = "1389328983354531840"
USERNAME = "chriscr ingle19"

client = SleeperClient()

PASS = "✓"
FAIL = "✗"


async def test(label, coro):
    try:
        result = await coro
        preview = str(result)[:120].replace("\n", " ")
        print(f"  {PASS} {label}: {preview}")
        return True
    except Exception as e:
        print(f"  {FAIL} {label}: {e}")
        return False


async def main():
    print("\n=== Sleeper MCP — Live API Tests ===\n")
    results = []

    print("── Core API ──")
    results.append(await test("NFL state", client.get_nfl_state()))
    results.append(await test("Get user", client.get_user(USERNAME)))
    results.append(await test("Get leagues", client.get_leagues(USER_ID)))
    results.append(await test("Get league", client.get_league(LEAGUE_ID)))
    results.append(await test("Get rosters", client.get_rosters(LEAGUE_ID)))
    results.append(await test("Get users", client.get_users(LEAGUE_ID)))
    results.append(await test("Get matchups week 1", client.get_matchups(LEAGUE_ID, 1)))
    results.append(await test("Get transactions week 1", client.get_transactions(LEAGUE_ID, 1)))
    results.append(await test("Get traded picks", client.get_traded_picks(LEAGUE_ID)))
    results.append(await test("Get winners bracket", client.get_winners_bracket(LEAGUE_ID)))
    results.append(await test("Get trending adds", client.get_trending(type_="add")))
    results.append(await test("Get trending drops", client.get_trending(type_="drop")))
    results.append(await test("Get league drafts", client.get_league_drafts(LEAGUE_ID)))

    print("\n── Player cache ──")
    results.append(await test("Get all players (cold)", client.get_players()))
    results.append(await test("Get all players (warm)", client.get_players()))

    print("\n── Undocumented stats endpoints ──")
    results.append(await test(
        "Season stats (teams)",
        client.get_season_stats("2026", positions=["TEAM"], order_by="pts_std")
    ))
    results.append(await test(
        "Weekly player stats wk1",
        client.get_player_stats(1, season="2026", positions=["QB", "RB", "WR"])
    ))
    results.append(await test(
        "Projections wk current",
        client.get_player_projections(4, season="2026", positions=["QB", "RB", "WR", "TE"])
    ))

    print("\n── MCP Tools ──")
    # Set context for tool tests
    from sleeper_mcp.cache import save_context
    save_context({"username": USERNAME, "user_id": USER_ID, "league_id": LEAGUE_ID})

    from sleeper_mcp.server import (
        get_context_info, get_my_team, get_standings, get_matchup,
        get_week_recap, get_free_agents, get_trending_players,
        search_player, get_playoff_picture, get_transactions,
        analyze_trade, get_waiver_targets, get_draft_recap, get_my_season,
    )

    results.append(await test("get_context_info", get_context_info()))
    results.append(await test("get_my_team", get_my_team()))
    results.append(await test("get_standings", get_standings()))
    results.append(await test("get_matchup (current)", get_matchup()))
    results.append(await test("get_week_recap (last)", get_week_recap()))
    results.append(await test("get_free_agents (WR)", get_free_agents("WR")))
    results.append(await test("get_trending_players", get_trending_players()))
    results.append(await test("search_player (Justin Jefferson)", search_player("Justin Jefferson")))
    results.append(await test("get_playoff_picture", get_playoff_picture()))
    results.append(await test("get_transactions", get_transactions()))
    results.append(await test(
        "analyze_trade",
        analyze_trade("Justin Jefferson", "CeeDee Lamb")
    ))
    results.append(await test("get_waiver_targets (RB)", get_waiver_targets("RB")))
    results.append(await test("get_draft_recap", get_draft_recap()))
    results.append(await test("get_my_season", get_my_season()))

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        sys.exit(1)


asyncio.run(main())
