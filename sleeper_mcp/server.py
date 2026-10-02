import os
from typing import Optional

from fastmcp import FastMCP

from .cache import load_context, save_context
from .client import SleeperClient

mcp = FastMCP("Sleeper Fantasy Football")

_token = os.getenv("SLEEPER_TOKEN")
client = SleeperClient(token=_token)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_context() -> dict:
    ctx = load_context()

    if not ctx.get("user_id"):
        username = ctx.get("username") or os.getenv("SLEEPER_USERNAME", "")
        if not username:
            raise ValueError(
                "No Sleeper username configured. "
                "Call set_context(username='your_sleeper_username') first."
            )
        user = await client.get_user(username)
        ctx["user_id"] = user["user_id"]
        ctx["username"] = user.get("display_name", username)
        save_context(ctx)

    if not ctx.get("league_id"):
        override = os.getenv("SLEEPER_LEAGUE_ID", "")
        if override:
            ctx["league_id"] = override
        else:
            leagues = await client.get_leagues(ctx["user_id"])
            if not leagues:
                raise ValueError("No NFL leagues found for this user.")
            ctx["league_id"] = leagues[0]["league_id"]
        save_context(ctx)

    return ctx


async def _current_week() -> tuple[int, str]:
    state = await client.get_nfl_state()
    return state.get("week", 1), state.get("season", "2026")


async def _build_roster_map(league_id: str) -> dict:
    rosters, users = await _gather(
        client.get_rosters(league_id),
        client.get_users(league_id),
    )
    user_map = {u["user_id"]: u for u in users}
    result = {}
    for r in rosters:
        owner = user_map.get(r.get("owner_id"), {})
        meta = owner.get("metadata") or {}
        team_name = meta.get("team_name") or owner.get("display_name") or f"Team {r['roster_id']}"
        s = r.get("settings") or {}
        result[r["roster_id"]] = {
            "roster_id": r["roster_id"],
            "owner_id": r.get("owner_id"),
            "team_name": team_name,
            "display_name": owner.get("display_name", ""),
            "starters": r.get("starters") or [],
            "players": r.get("players") or [],
            "reserve": r.get("reserve") or [],
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "fpts": s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100,
            "fpts_against": s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100,
            "waiver_position": s.get("waiver_position", 0),
            "waiver_budget_used": s.get("waiver_budget_used", 0),
        }
    return result


async def _gather(*coros):
    import asyncio
    return await asyncio.gather(*coros)


def _fmt_player(pid: str, players: dict, slot: Optional[str] = None) -> str:
    p = players.get(str(pid), {})
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
    pos = p.get("position", "?")
    team = p.get("team") or "FA"
    injury = p.get("injury_status") or ""
    bye = p.get("bye_week")
    parts = [f"{name} ({pos}, {team})"]
    if slot:
        parts = [f"[{slot}]"] + parts
    if injury:
        parts.append(f"[{injury}]")
    if bye:
        parts.append(f"Bye:Wk{bye}")
    return " ".join(parts)


def _find_player(name: str, players: dict) -> Optional[tuple[str, dict]]:
    needle = name.strip().lower().replace(" ", "")
    best = None
    best_rank = 9999
    for pid, p in players.items():
        full = f"{p.get('first_name', '')}{p.get('last_name', '')}".lower().replace(" ", "")
        search = (p.get("search_full_name") or "").lower()
        if needle in full or needle in search:
            rank = p.get("search_rank") or 9999
            if rank < best_rank:
                best_rank = rank
                best = (pid, p)
    return best


# ── Config tools ──────────────────────────────────────────────────────────────


@mcp.tool()
async def set_context(username: str, league_id: Optional[str] = None) -> str:
    """
    Configure your Sleeper username (and optionally a specific league ID).
    Run this once — settings persist across sessions.
    If no league_id is given, your most recent NFL league is selected automatically.
    """
    user = await client.get_user(username)
    if "user_id" not in user:
        return f"User '{username}' not found on Sleeper."

    ctx: dict = {
        "username": username,
        "user_id": user["user_id"],
        "display_name": user.get("display_name", username),
    }

    if league_id:
        ctx["league_id"] = league_id
    else:
        leagues = await client.get_leagues(user["user_id"])
        if not leagues:
            return f"No NFL leagues found for {username}."
        ctx["league_id"] = leagues[0]["league_id"]

    save_context(ctx)
    league = await client.get_league(ctx["league_id"])
    return (
        f"Context saved.\n"
        f"User: {user.get('display_name')} (@{username})\n"
        f"League: {league.get('name')} (Season {league.get('season')}, "
        f"{league.get('total_rosters')} teams)"
    )


@mcp.tool()
async def get_context_info() -> str:
    """Show the currently configured Sleeper user and league."""
    try:
        ctx = await _get_context()
        league = await client.get_league(ctx["league_id"])
        return (
            f"User: {ctx.get('display_name', ctx.get('username'))} "
            f"(ID: {ctx['user_id']})\n"
            f"League: {league.get('name')} (ID: {ctx['league_id']})\n"
            f"Season: {league.get('season')} | Status: {league.get('status')}\n"
            f"Teams: {league.get('total_rosters')}"
        )
    except ValueError as e:
        return str(e)


# ── My team ───────────────────────────────────────────────────────────────────


@mcp.tool()
async def get_my_team() -> str:
    """
    Show your full roster: starters, bench, and IR with injury flags and bye weeks.
    """
    ctx = await _get_context()
    roster_map, players = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_players(),
    )

    my = next((r for r in roster_map.values() if r["owner_id"] == ctx["user_id"]), None)
    if not my:
        return "Couldn't find your roster. Make sure you're a member of this league."

    starters = my["starters"]
    bench = [p for p in my["players"] if p not in starters and p not in my["reserve"]]

    lines = [
        f"** {my['team_name']} **",
        f"Record: {my['wins']}-{my['losses']}  |  Season pts: {my['fpts']:.2f}",
        "",
        "STARTERS:",
        *[f"  {_fmt_player(pid, players)}" for pid in starters if pid],
    ]
    if bench:
        lines += ["", "BENCH:", *[f"  {_fmt_player(pid, players)}" for pid in bench]]
    if my["reserve"]:
        lines += ["", "IR:", *[f"  {_fmt_player(pid, players)}" for pid in my["reserve"]]]

    return "\n".join(lines)


@mcp.tool()
async def get_my_season() -> str:
    """
    Full season summary: week-by-week results, record, points, and playoff outlook.
    """
    import asyncio

    ctx = await _get_context()
    week, season = await _current_week()
    roster_map, league = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_league(ctx["league_id"]),
    )

    my = next((r for r in roster_map.values() if r["owner_id"] == ctx["user_id"]), None)
    if not my:
        return "Couldn't find your team."

    playoff_spots = (league.get("settings") or {}).get("playoff_teams", 6)
    sorted_teams = sorted(roster_map.values(), key=lambda r: (r["wins"], r["fpts"]), reverse=True)
    my_rank = next((i + 1 for i, t in enumerate(sorted_teams) if t["roster_id"] == my["roster_id"]), "?")

    # Fetch all completed week matchups concurrently
    weeks_done = range(1, week)
    all_matchups = await asyncio.gather(
        *[client.get_matchups(ctx["league_id"], w) for w in weeks_done],
        return_exceptions=True,
    )

    results = []
    for w, matchups in zip(weeks_done, all_matchups):
        if isinstance(matchups, Exception):
            continue
        my_m = next((m for m in matchups if m["roster_id"] == my["roster_id"]), None)
        if not my_m:
            continue
        opp_m = next(
            (m for m in matchups if m.get("matchup_id") == my_m.get("matchup_id")
             and m["roster_id"] != my["roster_id"]),
            None,
        )
        my_pts = my_m.get("points") or 0
        opp_pts = (opp_m.get("points") or 0) if opp_m else 0
        opp_name = roster_map.get((opp_m or {}).get("roster_id", 0), {}).get("team_name", "?")
        results.append((w, my_pts, opp_pts, opp_name, my_pts > opp_pts))

    in_playoffs = isinstance(my_rank, int) and my_rank <= playoff_spots
    lines = [
        f"SEASON SUMMARY — {my['team_name']}",
        f"Record: {my['wins']}-{my['losses']}  |  Rank: #{my_rank} of {len(roster_map)}",
        f"Points For: {my['fpts']:.2f}  |  Points Against: {my['fpts_against']:.2f}",
        f"Playoffs: {'IN ✓' if in_playoffs else 'OUT'} (top {playoff_spots} qualify)",
        "=" * 44,
    ]
    for w, my_pts, opp_pts, opp_name, won in results:
        marker = "W" if won else "L"
        lines.append(f"  Wk{w:2} [{marker}] {my_pts:6.2f} – {opp_pts:6.2f}  vs {opp_name}")

    return "\n".join(lines)


@mcp.tool()
async def get_matchup(week: Optional[int] = None) -> str:
    """
    Your matchup for a given week (defaults to current week).
    Shows both teams' starters with per-player fantasy points and stats.
    """
    ctx = await _get_context()
    cur_week, season = await _current_week()
    if week is None:
        week = cur_week

    roster_map, matchups, players, league = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_matchups(ctx["league_id"], week),
        client.get_players(),
        client.get_league(ctx["league_id"]),
    )

    my = next((r for r in roster_map.values() if r["owner_id"] == ctx["user_id"]), None)
    if not my:
        return "Couldn't find your roster."

    my_m = next((m for m in matchups if m["roster_id"] == my["roster_id"]), None)
    if not my_m:
        return f"No matchup data for week {week}."

    opp_m = next(
        (m for m in matchups if m.get("matchup_id") == my_m.get("matchup_id")
         and m["roster_id"] != my["roster_id"]),
        None,
    )
    opp = roster_map.get((opp_m or {}).get("roster_id", 0), {}) if opp_m else {}

    if week < cur_week:
        status = "FINAL"
    elif week == cur_week:
        status = "LIVE"
    else:
        status = "UPCOMING"

    my_pts = my_m.get("points") or 0
    opp_pts = (opp_m.get("points") or 0) if opp_m else 0
    my_starters = my_m.get("starters") or []
    opp_starters = (opp_m.get("starters") or []) if opp_m else []

    # Per-player points: try exact player_map first (needs token), fall back to GQL weekly stats
    player_pts: dict = {}
    scoring_settings = league.get("scoring_settings") or {}
    pts_field = client.scoring_type(scoring_settings)

    if status != "UPCOMING":
        all_starter_ids = [p for p in (my_starters + opp_starters) if p]
        # Try matchup_legs (exact, requires token)
        player_map_by_roster = await client.get_matchup_player_map(ctx["league_id"], week)
        if player_map_by_roster:
            for roster_player_map in player_map_by_roster.values():
                player_pts.update(roster_player_map or {})
        else:
            # Fall back to GQL weekly_stats (no token needed, uses scoring type)
            try:
                week_stats = await client.get_stats_for_players(all_starter_ids, week, season)
                for pid, stats in week_stats.items():
                    player_pts[pid] = stats.get(pts_field, 0) or 0
            except Exception:
                pass

    def fmt_starter(pid):
        p = players.get(str(pid), {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        injury = p.get("injury_status") or ""
        injury_s = f" [{injury}]" if injury else ""
        pts = player_pts.get(str(pid)) or player_pts.get(pid)
        pts_s = f"  {pts:5.2f} pts" if pts is not None and status != "UPCOMING" else ""
        return f"  {name} ({pos}, {team}){injury_s}{pts_s}"

    lines = [
        f"Week {week} Matchup  [{status}]",
        f"",
        f"  {my['team_name']:<28} {my_pts:>7.2f} pts",
        f"  {'vs':>28}",
        f"  {opp.get('team_name', 'Opponent'):<28} {opp_pts:>7.2f} pts",
        "",
        "YOUR STARTERS:",
        *[fmt_starter(pid) for pid in my_starters if pid],
    ]
    if opp_m:
        lines += [
            "",
            "OPPONENT STARTERS:",
            *[fmt_starter(pid) for pid in opp_starters if pid],
        ]
    if player_pts and status != "UPCOMING":
        src = "Sleeper scoring" if player_map_by_roster else pts_field.replace("pts_", "").upper() + " scoring"
        lines.append(f"\n(Points source: {src})")
    return "\n".join(lines)


# ── League ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def get_standings() -> str:
    """Current league standings: rank, record, points for/against for every team."""
    ctx = await _get_context()
    roster_map = await _build_roster_map(ctx["league_id"])
    league = await client.get_league(ctx["league_id"])
    playoff_spots = (league.get("settings") or {}).get("playoff_teams", 6)

    sorted_teams = sorted(roster_map.values(), key=lambda r: (r["wins"], r["fpts"]), reverse=True)

    lines = ["LEAGUE STANDINGS", "=" * 50]
    for i, t in enumerate(sorted_teams, 1):
        rec = f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t["ties"] else "")
        marker = " ←" if i == playoff_spots else ""
        lines.append(f"  {i:2}. {t['team_name']:<24} {rec:<8} {t['fpts']:>8.2f} pts{marker}")
    return "\n".join(lines)


@mcp.tool()
async def get_playoff_picture() -> str:
    """
    Who's in, who's on the bubble, who's out — with games remaining and points gap.
    """
    ctx = await _get_context()
    roster_map, league = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_league(ctx["league_id"]),
    )
    week, _ = await _current_week()

    settings = league.get("settings") or {}
    playoff_spots = settings.get("playoff_teams", 6)
    playoff_start = settings.get("playoff_week_start", 15)
    weeks_left = max(0, playoff_start - 1 - week)

    sorted_teams = sorted(roster_map.values(), key=lambda r: (r["wins"], r["fpts"]), reverse=True)
    bubble_team = sorted_teams[playoff_spots - 1] if len(sorted_teams) >= playoff_spots else None

    lines = [
        f"PLAYOFF PICTURE  (top {playoff_spots} qualify | {weeks_left} weeks left)",
        "=" * 50,
    ]
    for i, t in enumerate(sorted_teams, 1):
        rec = f"{t['wins']}-{t['losses']}"
        status = "IN " if i <= playoff_spots else "OUT"
        gap = ""
        if i > playoff_spots and bubble_team:
            win_gap = bubble_team["wins"] - t["wins"]
            pts_gap = bubble_team["fpts"] - t["fpts"]
            gap = f"  ({win_gap}W / {pts_gap:+.1f} pts behind)"
        if i == playoff_spots:
            lines.append("  " + "─" * 46)
        lines.append(f"  [{status}] {i:2}. {t['team_name']:<22} {rec:<8} {t['fpts']:>7.1f}{gap}")

    return "\n".join(lines)


@mcp.tool()
async def get_week_recap(week: Optional[int] = None) -> str:
    """All matchup results for a given week (defaults to last completed week)."""
    ctx = await _get_context()
    cur_week, _ = await _current_week()
    if week is None:
        week = max(1, cur_week - 1)

    matchups, roster_map = await _gather(
        client.get_matchups(ctx["league_id"], week),
        _build_roster_map(ctx["league_id"]),
    )

    pairs: dict = {}
    for m in matchups:
        mid = m.get("matchup_id")
        pairs.setdefault(mid, []).append(m)

    status = "FINAL" if week < cur_week else ("LIVE" if week == cur_week else "UPCOMING")
    lines = [f"Week {week} Results  [{status}]", "=" * 44]

    for pair in sorted(pairs.values(), key=lambda p: p[0].get("matchup_id", 0)):
        if len(pair) != 2:
            continue
        a, b = sorted(pair, key=lambda m: m.get("points") or 0, reverse=True)
        name_a = roster_map.get(a["roster_id"], {}).get("team_name", f"Team {a['roster_id']}")
        name_b = roster_map.get(b["roster_id"], {}).get("team_name", f"Team {b['roster_id']}")
        pts_a = a.get("points") or 0
        pts_b = b.get("points") or 0
        lines.append(f"  {name_a:<26} {pts_a:>7.2f}")
        lines.append(f"  {name_b:<26} {pts_b:>7.2f}")
        lines.append("")

    return "\n".join(lines)


# ── Players & Moves ───────────────────────────────────────────────────────────


@mcp.tool()
async def get_free_agents(position: Optional[str] = None) -> str:
    """
    Available free agents, sorted by depth chart / relevance.
    position: QB | RB | WR | TE | K | DEF  (omit for all)
    """
    ctx = await _get_context()
    rosters, players = await _gather(
        client.get_rosters(ctx["league_id"]),
        client.get_players(),
    )

    rostered: set = set()
    for r in rosters:
        rostered.update(r.get("players") or [])
        rostered.update(r.get("reserve") or [])

    positions = [position.upper()] if position else ["QB", "RB", "WR", "TE", "K", "DEF"]
    lines = [f"FREE AGENTS" + (f" — {position.upper()}" if position else ""), "=" * 44]

    for pos in positions:
        available = []
        for pid, p in players.items():
            if pid in rostered:
                continue
            player_pos = p.get("position") or ""
            fantasy_pos = p.get("fantasy_positions") or []
            if player_pos != pos and pos not in fantasy_pos:
                continue
            if not p.get("team"):
                continue
            available.append((pid, p))

        available.sort(key=lambda x: (
            1 if x[1].get("injury_status") else 0,
            x[1].get("depth_chart_order") or 99,
            x[1].get("search_rank") or 9999,
        ))

        if not available:
            continue
        lines.append(f"\n{pos}:")
        for pid, p in available[:12]:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            team = p.get("team", "?")
            depth = p.get("depth_chart_position") or ""
            injury = p.get("injury_status") or ""
            depth_s = f" ({depth})" if depth else ""
            injury_s = f" [{injury}]" if injury else ""
            lines.append(f"  {name} ({team}){depth_s}{injury_s}")

    return "\n".join(lines)


@mcp.tool()
async def get_trending_players(type: str = "add", hours: int = 24, limit: int = 20) -> str:
    """
    Hottest adds or drops across all Sleeper leagues.
    type: 'add' or 'drop'  |  hours: lookback window  |  limit: number of results
    """
    trending, players = await _gather(
        client.get_trending(type_=type, hours=hours, limit=limit),
        client.get_players(),
    )

    label = "TRENDING ADDS" if type == "add" else "TRENDING DROPS"
    lines = [f"{label}  (last {hours}h)", "=" * 44]
    for item in trending:
        pid = item.get("player_id", "")
        count = item.get("count", 0)
        p = players.get(str(pid), {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        injury = p.get("injury_status") or ""
        injury_s = f" [{injury}]" if injury else ""
        lines.append(f"  {name} ({pos}, {team}){injury_s}  —  {count:,} {type}s")
    return "\n".join(lines)


@mcp.tool()
async def search_player(name: str) -> str:
    """
    Look up a player: position, team, injury status, depth chart, age.
    """
    players = await client.get_players()
    result = _find_player(name, players)

    if not result:
        # Try broader search
        needle = name.strip().lower()
        matches = [
            (pid, p) for pid, p in players.items()
            if needle in (p.get("search_full_name") or "").lower()
            or needle in f"{p.get('first_name', '')} {p.get('last_name', '')}".lower()
        ]
        matches.sort(key=lambda x: x[1].get("search_rank") or 9999)
        if not matches:
            return f"No player found matching '{name}'."
        results = matches[:5]
    else:
        results = [result] + []

    lines = [f"Player search: '{name}'", "=" * 44]
    for pid, p in (results if not result else [(result[0], result[1])]):
        fname = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        lines += [
            f"\n{fname}",
            f"  {p.get('position', '?')} | {p.get('team') or 'Free Agent'} | "
            f"Age {p.get('age', '?')} | {p.get('years_exp', '?')} yrs exp",
            f"  Status: {p.get('status', 'Unknown')}"
            + (f"  [{p.get('injury_status')}]" if p.get("injury_status") else ""),
        ]
        if p.get("depth_chart_position"):
            lines.append(f"  Depth: {p['depth_chart_position']} (#{p.get('depth_chart_order', '?')})")
        if p.get("injury_notes"):
            lines.append(f"  Notes: {p['injury_notes']}")

    return "\n".join(lines)


@mcp.tool()
async def get_player_game_stats(name: str, week: Optional[int] = None) -> str:
    """
    Detailed stats and fantasy points for a specific player in a given week.
    Shows rushing, receiving, passing yards, TDs, and pre-calculated PPR/std points.
    week defaults to last completed week.
    """
    cur_week, season = await _current_week()
    if week is None:
        week = max(1, cur_week - 1)

    players = await client.get_players()
    result = _find_player(name, players)
    if not result:
        return f"Player '{name}' not found."

    pid, p = result
    full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    pos = p.get("position", "?")
    team = p.get("team") or "FA"

    stats_data = await client.get_stats_for_players([pid], week, season)
    stats = stats_data.get(pid) or stats_data.get(str(pid)) or {}

    if not stats:
        return f"{full_name} ({pos}, {team}) — no stats found for Week {week}."

    pts_ppr = stats.get("pts_ppr", 0) or 0
    pts_std = stats.get("pts_std", 0) or 0
    pts_half = stats.get("pts_half_ppr", 0) or 0
    gp = int(stats.get("gp", 0) or 0)

    lines = [
        f"{full_name} ({pos}, {team}) — Week {week}, {season}",
        f"Fantasy Points: {pts_ppr:.2f} PPR  |  {pts_half:.2f} Half  |  {pts_std:.2f} Std",
        "=" * 44,
    ]

    if not gp:
        lines.append("Did not play this week.")
        return "\n".join(lines)

    # Passing
    pass_att = stats.get("pass_att", 0) or 0
    if pass_att:
        pass_cmp = stats.get("pass_cmp", 0) or 0
        pass_yd = stats.get("pass_yd", 0) or 0
        pass_td = int(stats.get("pass_td", 0) or 0)
        pass_int = int(stats.get("pass_int", 0) or 0)
        pass_lng = stats.get("pass_lng", 0) or 0
        cmp_pct = stats.get("cmp_pct", 0) or 0
        lines.append(f"Passing: {pass_cmp}/{pass_att} ({cmp_pct:.0f}%)  {pass_yd:.0f} yds  {pass_td} TD  {pass_int} INT  Long: {pass_lng:.0f}")

    # Rushing
    rush_att = stats.get("rush_att", 0) or 0
    if rush_att:
        rush_yd = stats.get("rush_yd", 0) or 0
        rush_td = int(stats.get("rush_td", 0) or 0)
        rush_lng = stats.get("rush_lng", 0) or 0
        rush_ypa = stats.get("rush_ypa", 0) or 0
        lines.append(f"Rushing: {rush_att:.0f} att  {rush_yd:.0f} yds  {rush_td} TD  Long: {rush_lng:.0f}  YPA: {rush_ypa:.1f}")

    # Receiving
    rec = stats.get("rec", 0) or 0
    if rec or stats.get("rec_tgt", 0):
        rec_tgt = stats.get("rec_tgt", 0) or 0
        rec_yd = stats.get("rec_yd", 0) or 0
        rec_td = int(stats.get("rec_td", 0) or 0)
        rec_lng = stats.get("rec_lng", 0) or 0
        rec_ypr = stats.get("rec_ypr", 0) or 0
        lines.append(f"Receiving: {rec:.0f} rec / {rec_tgt:.0f} tgt  {rec_yd:.0f} yds  {rec_td} TD  Long: {rec_lng:.0f}  YPR: {rec_ypr:.1f}")

    # Defense (DST)
    sack = stats.get("sack", 0) or 0
    if sack or stats.get("def_td", 0) or stats.get("pts_allow", 0):
        def_td = int(stats.get("def_td", 0) or 0)
        int_td = int(stats.get("int_td", 0) or 0)
        fum_rec = int(stats.get("fum_rec", 0) or 0)
        safe = int(stats.get("safe", 0) or 0)
        pts_allow = stats.get("pts_allow", 0) or 0
        yds_allow = stats.get("yds_allow", 0) or 0
        lines.append(f"Defense: {sack:.0f} sacks  {def_td + int_td} TD  {fum_rec} FR  {safe} saf  {pts_allow:.0f} pts allowed  {yds_allow:.0f} yds allowed")

    # Kicker
    fgm = stats.get("fgm", 0) or 0
    if fgm or stats.get("fga", 0):
        fga = stats.get("fga", 0) or 0
        fgm_lng = stats.get("fgm_lng", 0) or 0
        xpm = stats.get("xpm", 0) or 0
        xpa = stats.get("xpa", 0) or 0
        lines.append(f"Kicking: {fgm:.0f}/{fga:.0f} FG  Long: {fgm_lng:.0f}  {xpm:.0f}/{xpa:.0f} XP")

    # Snap count
    off_snp = stats.get("off_snp", 0) or 0
    tm_snp = stats.get("tm_off_snp", 0) or 0
    if off_snp and tm_snp:
        snp_pct = (off_snp / tm_snp * 100) if tm_snp else 0
        lines.append(f"Snaps: {int(off_snp)}/{int(tm_snp)} ({snp_pct:.0f}%)")

    # Bonuses
    bonus_keys = [k for k in stats if k.startswith("bonus_")]
    if bonus_keys:
        bonuses = [f"{k.replace('bonus_', '')}: {stats[k]:.0f}" for k in bonus_keys]
        lines.append(f"Bonuses: {', '.join(bonuses)}")

    return "\n".join(lines)


@mcp.tool()
async def get_player_season_stats(name: str) -> str:
    """
    Season-to-date stats and fantasy points for any player.
    Shows cumulative rushing, receiving, passing stats and scoring rank.
    """
    cur_week, season = await _current_week()
    players, season_map = await _gather(
        client.get_players(),
        client.get_season_stats_map(season),
    )

    result = _find_player(name, players)
    if not result:
        return f"Player '{name}' not found."

    pid, p = result
    full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    pos = p.get("position", "?")
    team = p.get("team") or "FA"

    totals = season_map.get(str(pid)) or {}
    if not totals or not totals.get("pts_ppr"):
        return f"{full_name} — no stats found for {season} season."

    games_played = int(totals.get("gp", 0) or 0)
    pts_ppr = totals.get("pts_ppr", 0) or 0
    pts_std = totals.get("pts_std", 0) or 0
    pts_half = totals.get("pts_half_ppr", 0) or 0
    ppg_ppr = pts_ppr / games_played if games_played else 0

    lines = [
        f"{full_name} ({pos}, {team}) — {season} Season ({games_played} games)",
        f"Fantasy Points: {pts_ppr:.2f} PPR  |  {pts_half:.2f} Half  |  {pts_std:.2f} Std",
        f"PPR per game: {ppg_ppr:.2f}",
        "=" * 44,
    ]

    pass_att = totals.get("pass_att", 0) or 0
    if pass_att:
        pass_cmp = totals.get("pass_cmp", 0) or 0
        pass_yd = totals.get("pass_yd", 0) or 0
        pass_td = int(totals.get("pass_td", 0) or 0)
        pass_int = int(totals.get("pass_int", 0) or 0)
        cmp_pct = (pass_cmp / pass_att * 100) if pass_att else 0
        lines.append(f"Passing: {pass_cmp:.0f}/{pass_att:.0f} ({cmp_pct:.1f}%)  {pass_yd:.0f} yds  {pass_td} TD  {pass_int} INT")

    rush_att = totals.get("rush_att", 0) or 0
    if rush_att:
        rush_yd = totals.get("rush_yd", 0) or 0
        rush_td = int(totals.get("rush_td", 0) or 0)
        rush_ypa = rush_yd / rush_att if rush_att else 0
        lines.append(f"Rushing: {rush_att:.0f} att  {rush_yd:.0f} yds  {rush_td} TD  YPA: {rush_ypa:.1f}")

    rec = totals.get("rec", 0) or 0
    if rec or totals.get("rec_tgt", 0):
        rec_tgt = totals.get("rec_tgt", 0) or 0
        rec_yd = totals.get("rec_yd", 0) or 0
        rec_td = int(totals.get("rec_td", 0) or 0)
        rec_ypr = rec_yd / rec if rec else 0
        lines.append(f"Receiving: {rec:.0f} rec / {rec_tgt:.0f} tgt  {rec_yd:.0f} yds  {rec_td} TD  YPR: {rec_ypr:.1f}")

    return "\n".join(lines)


@mcp.tool()
async def get_transactions(week: Optional[int] = None, num_weeks: int = 1) -> str:
    """
    Recent adds, drops, waiver claims, and trades in plain language.
    week: defaults to current week  |  num_weeks: how many weeks back to show
    """
    import asyncio

    ctx = await _get_context()
    cur_week, _ = await _current_week()
    if week is None:
        week = cur_week

    weeks = list(range(max(1, week - num_weeks + 1), week + 1))
    results = await asyncio.gather(
        *[client.get_transactions(ctx["league_id"], w) for w in weeks],
        return_exceptions=True,
    )

    players, roster_map = await _gather(client.get_players(), _build_roster_map(ctx["league_id"]))

    all_txns = []
    for w, txns in zip(weeks, results):
        if isinstance(txns, Exception):
            continue
        for t in txns:
            t["_week"] = w
            all_txns.append(t)

    all_txns.sort(key=lambda t: t.get("status_updated") or 0, reverse=True)

    def pname(pid):
        p = players.get(str(pid), {})
        return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)

    def tname(rid):
        return roster_map.get(rid, {}).get("team_name", f"Team {rid}")

    lines = [f"Transactions — Week{'s ' + str(weeks[0]) + '–' if len(weeks) > 1 else ' '}{week}", "=" * 44]

    if not all_txns:
        lines.append("No transactions found.")
        return "\n".join(lines)

    for t in all_txns:
        ttype = t.get("type", "")
        rids = t.get("roster_ids") or []
        adds = t.get("adds") or {}
        drops = t.get("drops") or {}
        picks = t.get("draft_picks") or []
        faab = t.get("waiver_budget") or []
        wk = t.get("_week", "")

        if ttype == "trade":
            teams = " ↔ ".join(tname(r) for r in rids)
            lines.append(f"[Wk{wk}] TRADE: {teams}")
            for pid, rid in adds.items():
                lines.append(f"  {tname(rid)} receives: {pname(pid)}")
            for pick in picks:
                lines.append(f"  {tname(pick['owner_id'])} receives: {pick['season']} Rd{pick['round']} pick")
            for f in faab:
                lines.append(f"  ${f['amount']} FAAB: {tname(f['sender'])} → {tname(f['receiver'])}")

        elif ttype == "waiver":
            rid = rids[0] if rids else 0
            bid = (t.get("settings") or {}).get("waiver_bid", 0)
            bid_s = f" (${bid} FAAB)" if bid else ""
            lines.append(f"[Wk{wk}] WAIVER{bid_s}: {tname(rid)}")
            for pid in adds:
                lines.append(f"  + {pname(pid)}")
            for pid in drops:
                lines.append(f"  - {pname(pid)}")

        elif ttype == "free_agent":
            rid = rids[0] if rids else 0
            lines.append(f"[Wk{wk}] FA: {tname(rid)}")
            for pid in adds:
                lines.append(f"  + {pname(pid)}")
            for pid in drops:
                lines.append(f"  - {pname(pid)}")

        lines.append("")

    return "\n".join(lines)


# ── Analytics ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def analyze_trade(giving: str, receiving: str) -> str:
    """
    Analyze a potential trade. Give comma-separated player names for each side.

    Example: giving="Ja'Marr Chase, Travis Kelce" receiving="CeeDee Lamb, George Kittle"
    """
    players = await client.get_players()

    def summarize(pid, p):
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        age = p.get("age", "?")
        depth = p.get("depth_chart_position") or ""
        injury = p.get("injury_status") or ""
        rank = p.get("search_rank")
        s = f"  {name} ({pos}, {team}) | Age {age}"
        if rank:
            s += f" | Rank #{rank}"
        if depth:
            s += f" | {depth}"
        if injury:
            s += f" | ⚠ {injury}"
        return s

    give_names = [n.strip() for n in giving.split(",")]
    get_names = [n.strip() for n in receiving.split(",")]

    give_found = [_find_player(n, players) for n in give_names]
    get_found = [_find_player(n, players) for n in get_names]

    lines = ["TRADE ANALYSIS", "=" * 44, "\nYOU GIVE UP:"]
    give_data = []
    for i, r in enumerate(give_found):
        if r:
            lines.append(summarize(*r))
            give_data.append(r[1])
        else:
            lines.append(f"  ⚠ '{give_names[i]}' not found")

    lines.append("\nYOU RECEIVE:")
    get_data = []
    for i, r in enumerate(get_found):
        if r:
            lines.append(summarize(*r))
            get_data.append(r[1])
        else:
            lines.append(f"  ⚠ '{get_names[i]}' not found")

    lines.append("\nANALYSIS:")

    # Positional balance
    pos_delta: dict = {}
    for p in give_data:
        pos = p.get("position", "?")
        pos_delta[pos] = pos_delta.get(pos, 0) - 1
    for p in get_data:
        pos = p.get("position", "?")
        pos_delta[pos] = pos_delta.get(pos, 0) + 1
    for pos, d in pos_delta.items():
        if d > 0:
            lines.append(f"  + You gain at {pos}")
        elif d < 0:
            lines.append(f"  - You lose depth at {pos}")

    # Age delta
    give_ages = [p.get("age") or 0 for p in give_data if p.get("age")]
    get_ages = [p.get("age") or 0 for p in get_data if p.get("age")]
    if give_ages and get_ages:
        avg_give = sum(give_ages) / len(give_ages)
        avg_get = sum(get_ages) / len(get_ages)
        if avg_get < avg_give - 1:
            lines.append(f"  + Younger incoming (avg {avg_get:.0f} vs {avg_give:.0f})")
        elif avg_get > avg_give + 1:
            lines.append(f"  - Older incoming (avg {avg_get:.0f} vs {avg_give:.0f})")

    # Rank delta
    give_ranks = [p.get("search_rank") or 999 for p in give_data]
    get_ranks = [p.get("search_rank") or 999 for p in get_data]
    if give_ranks and get_ranks:
        avg_give_rank = sum(give_ranks) / len(give_ranks)
        avg_get_rank = sum(get_ranks) / len(get_ranks)
        if avg_get_rank < avg_give_rank - 20:
            lines.append(f"  + You're getting higher-ranked players (#{avg_get_rank:.0f} vs #{avg_give_rank:.0f})")
        elif avg_get_rank > avg_give_rank + 20:
            lines.append(f"  - You're getting lower-ranked players (#{avg_get_rank:.0f} vs #{avg_give_rank:.0f})")

    # Injury flags
    inj_give = [f"{p.get('first_name','')} {p.get('last_name','')}".strip() for p in give_data if p.get("injury_status")]
    inj_get = [f"{p.get('first_name','')} {p.get('last_name','')}".strip() for p in get_data if p.get("injury_status")]
    if inj_give:
        lines.append(f"  ⚠ Selling injured: {', '.join(inj_give)} (potential sell-high leverage)")
    if inj_get:
        lines.append(f"  ⚠ Buying injured: {', '.join(inj_get)} (buy-low risk/reward)")

    # Depth chart flags
    for p in get_data:
        if p.get("depth_chart_order", 1) > 1:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            lines.append(f"  ⚠ {name} is not a starter on their NFL team")

    lines.append(
        "\nTip: Ask me about each player's stats, schedule, or injury timeline for deeper analysis."
    )
    return "\n".join(lines)


@mcp.tool()
async def get_waiver_targets(position: Optional[str] = None) -> str:
    """
    Best waiver wire pickups based on your roster's needs + trending data.
    position: QB | RB | WR | TE | K | DEF  (omit for all)
    """
    ctx = await _get_context()
    rosters, players, trending_raw = await _gather(
        client.get_rosters(ctx["league_id"]),
        client.get_players(),
        client.get_trending(type_="add", hours=48, limit=100),
    )

    my = next((r for r in rosters if r.get("owner_id") == ctx["user_id"]), None)
    if not my:
        return "Couldn't find your roster."

    rostered: set = set()
    for r in rosters:
        rostered.update(r.get("players") or [])
        rostered.update(r.get("reserve") or [])

    my_pos_counts: dict = {}
    for pid in (my.get("players") or []):
        p = players.get(str(pid), {})
        pos = p.get("position") or ""
        my_pos_counts[pos] = my_pos_counts.get(pos, 0) + 1

    trending_map = {item["player_id"]: item["count"] for item in trending_raw}

    positions = [position.upper()] if position else ["QB", "RB", "WR", "TE", "K", "DEF"]
    needs = {"QB": 2, "RB": 3, "WR": 3, "TE": 2}

    lines = ["WAIVER TARGETS", "=" * 44]

    for pos in positions:
        available = []
        for pid, p in players.items():
            if pid in rostered:
                continue
            if (p.get("position") or "") != pos:
                continue
            if not p.get("team"):
                continue
            trend = trending_map.get(pid, 0)
            rank = p.get("search_rank") or 9999
            available.append((pid, p, trend, rank))

        available.sort(key=lambda x: (-x[2], x[3]))
        top = available[:8]
        if not top:
            continue

        need = my_pos_counts.get(pos, 0) < needs.get(pos, 1)
        need_tag = "  ← NEED" if need else ""
        lines.append(f"\n{pos}{need_tag}:")
        for pid, p, trend, rank in top:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            team = p.get("team", "?")
            depth = p.get("depth_chart_position") or ""
            injury = p.get("injury_status") or ""
            trend_s = f"  🔥 {trend:,} adds" if trend > 50 else ""
            depth_s = f" ({depth})" if depth else ""
            injury_s = f" [{injury}]" if injury else ""
            lines.append(f"  {name} ({team}){depth_s}{injury_s}{trend_s}")

    return "\n".join(lines)


@mcp.tool()
async def get_schedule_strength() -> str:
    """
    Remaining schedule difficulty for every team — useful for identifying
    trade targets (teams with easy schedules) and streaming decisions.
    Based on each team's average points scored this season.
    """
    import asyncio

    ctx = await _get_context()
    cur_week, _ = await _current_week()
    roster_map, league = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_league(ctx["league_id"]),
    )

    playoff_start = (league.get("settings") or {}).get("playoff_week_start", 15)
    reg_weeks = playoff_start - 1

    # Fetch all completed weeks to compute team scoring averages
    past_weeks = list(range(1, cur_week))
    if not past_weeks:
        return "No completed weeks yet — check back after Week 1."

    past_results = await asyncio.gather(
        *[client.get_matchups(ctx["league_id"], w) for w in past_weeks],
        return_exceptions=True,
    )

    team_scores: dict = {rid: [] for rid in roster_map}
    for matchups in past_results:
        if isinstance(matchups, Exception):
            continue
        for m in matchups:
            rid = m.get("roster_id")
            if rid in team_scores:
                team_scores[rid].append(m.get("points") or 0)

    avg_score: dict = {
        rid: (sum(scores) / len(scores) if scores else 0)
        for rid, scores in team_scores.items()
    }

    # Fetch remaining weeks to compute future opponent strength
    future_weeks = list(range(cur_week, reg_weeks + 1))
    if not future_weeks:
        return "Regular season is over — playoffs are underway."

    future_results = await asyncio.gather(
        *[client.get_matchups(ctx["league_id"], w) for w in future_weeks],
        return_exceptions=True,
    )

    future_opp_strength: dict = {rid: [] for rid in roster_map}
    for matchups in future_results:
        if isinstance(matchups, Exception):
            continue
        pairs: dict = {}
        for m in matchups:
            pairs.setdefault(m.get("matchup_id"), []).append(m)
        for pair in pairs.values():
            if len(pair) == 2:
                a, b = pair
                if a["roster_id"] in future_opp_strength:
                    future_opp_strength[a["roster_id"]].append(avg_score.get(b["roster_id"], 0))
                if b["roster_id"] in future_opp_strength:
                    future_opp_strength[b["roster_id"]].append(avg_score.get(a["roster_id"], 0))

    schedule_data = []
    for rid, strengths in future_opp_strength.items():
        avg_opp = sum(strengths) / len(strengths) if strengths else 0
        t = roster_map.get(rid, {})
        schedule_data.append((rid, t, avg_opp))

    schedule_data.sort(key=lambda x: x[2])
    league_avg = sum(avg_score.values()) / len(avg_score) if avg_score else 100

    lines = [
        f"REMAINING SCHEDULE STRENGTH  (Weeks {cur_week}–{reg_weeks})",
        "Easier schedules listed first  |  Based on opponent avg pts/week",
        "=" * 50,
    ]
    for rid, t, avg_opp in schedule_data:
        rec = f"{t['wins']}-{t['losses']}"
        diff = avg_opp - league_avg
        tag = "EASY" if diff < -5 else ("HARD" if diff > 5 else "AVG ")
        lines.append(
            f"  [{tag}] {t.get('team_name', '?'):<24} {rec:<8} Opp avg: {avg_opp:5.1f}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_draft_recap() -> str:
    """
    Your league's draft: first 5 rounds with picks, keepers, and any outstanding traded picks.
    """
    ctx = await _get_context()
    roster_map, drafts = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_league_drafts(ctx["league_id"]),
    )

    if not drafts:
        return "No drafts found for this league."

    draft = drafts[0]
    picks, traded = await _gather(
        client.get_draft_picks(draft["draft_id"]),
        client.get_traded_picks(ctx["league_id"]),
    )

    meta = draft.get("metadata") or {}
    settings = draft.get("settings") or {}
    lines = [
        f"DRAFT RECAP — {meta.get('name', 'Draft')}",
        f"Type: {draft.get('type', '?').title()}  |  Status: {draft.get('status', '?').title()}  "
        f"|  Season: {draft.get('season')}  |  Rounds: {settings.get('rounds', '?')}",
        "=" * 50,
    ]

    by_round: dict = {}
    for pick in picks:
        by_round.setdefault(pick.get("round", 0), []).append(pick)

    for r in sorted(by_round.keys())[:5]:
        lines.append(f"\nRound {r}:")
        for pick in sorted(by_round[r], key=lambda p: p.get("pick_no") or 0):
            m = pick.get("metadata") or {}
            name = f"{m.get('first_name', '')} {m.get('last_name', '')}".strip() or "?"
            pos = m.get("position", "?")
            team = m.get("team", "?")
            pick_no = pick.get("pick_no", "?")
            rid = pick.get("roster_id")
            manager = roster_map.get(int(rid) if rid else 0, {}).get("team_name", "?")
            keeper = " [KEEPER]" if pick.get("is_keeper") else ""
            lines.append(f"  #{pick_no:<4} {name} ({pos}, {team}) → {manager}{keeper}")

    remaining = len(by_round) - 5
    if remaining > 0:
        lines.append(f"\n  ...and {remaining} more round(s)")

    if traded:
        lines.append(f"\nOUTSTANDING TRADED PICKS ({len(traded)}):")
        for tp in traded[:15]:
            orig = roster_map.get(tp.get("roster_id"), {}).get("team_name", "?")
            curr = roster_map.get(tp.get("owner_id"), {}).get("team_name", "?")
            lines.append(f"  {tp.get('season')} Rd{tp.get('round')}: {orig}'s pick → {curr}")

    return "\n".join(lines)


# ── Player news & outlook ─────────────────────────────────────────────────────


@mcp.tool()
async def get_player_news(name: str) -> str:
    """
    Recent news, injury updates, game recaps, and season outlook for any player.
    Pulls the last several news items plus a long-form analyst outlook.
    """
    players = await client.get_players()
    result = _find_player(name, players)
    if not result:
        return f"Player '{name}' not found."

    pid, p = result
    full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    pos = p.get("position", "?")
    team = p.get("team") or "FA"

    news_items, outlook = await _gather(
        client.get_player_news_gql(pid),
        client.get_player_outlook_gql(pid),
    )

    lines = [f"{full_name} ({pos}, {team})", "=" * 44]

    injury = p.get("injury_status", "")
    notes = p.get("injury_notes", "")
    if injury:
        lines.append(f"Status: {injury}" + (f" — {notes}" if notes else ""))
    lines.append("")

    if outlook:
        meta = outlook.get("metadata") or {}
        analysis = meta.get("analysis") or meta.get("description") or ""
        if analysis:
            lines += ["SEASON OUTLOOK:", analysis[:600], ""]

    if news_items:
        lines.append("RECENT NEWS:")
        for item in news_items:
            meta = item.get("metadata") or {}
            desc = meta.get("description") or ""
            analysis = meta.get("analysis") or ""
            source = item.get("source") or ""
            pub = item.get("published") or ""
            if pub:
                try:
                    from datetime import datetime
                    ts = int(pub) / 1000
                    pub = datetime.fromtimestamp(ts).strftime("%b %d")
                except Exception:
                    pass
            lines.append(f"[{pub}] {source}")
            if desc:
                lines.append(f"  {desc[:200]}")
            if analysis:
                lines.append(f"  → {analysis[:150]}")
            lines.append("")

    if not news_items and not outlook:
        lines.append("No recent news found.")

    return "\n".join(lines)


# ── Projections ───────────────────────────────────────────────────────────────


@mcp.tool()
async def get_player_stats_profile(name: str) -> str:
    """
    Full fantasy scoring profile for any player: season totals, per-game average,
    and the last 2 weeks of actual stats. Use this for start/sit context.
    """
    cur_week, season = await _current_week()

    players, season_map = await _gather(
        client.get_players(),
        client.get_season_stats_map(season),
    )

    result = _find_player(name, players)
    if not result:
        return f"Player '{name}' not found."

    pid, p = result
    full_name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    pos = p.get("position", "?")
    team = p.get("team") or "FA"

    s = season_map.get(str(pid)) or {}
    pts_ppr = s.get("pts_ppr", 0) or 0
    gp = int(s.get("gp", 0) or 0)
    ppg = pts_ppr / gp if gp else 0

    lines = [
        f"{full_name} ({pos}, {team}) — {season} Season",
        f"PPR Total: {pts_ppr:.2f}  |  {ppg:.2f}/game  ({gp} games)",
        "=" * 44,
    ]

    injury = p.get("injury_status", "")
    if injury:
        lines.append(f"⚠ Status: {injury}")

    if s.get("pass_att", 0):
        pa = s.get("pass_att", 0); pc = s.get("pass_cmp", 0)
        lines.append(f"Passing: {pc:.0f}/{pa:.0f}  {s.get('pass_yd', 0):.0f} yds  {int(s.get('pass_td', 0))} TD  {int(s.get('pass_int', 0))} INT")
    if s.get("rush_att", 0):
        lines.append(f"Rushing: {s.get('rush_att', 0):.0f} att  {s.get('rush_yd', 0):.0f} yds  {int(s.get('rush_td', 0))} TD")
    if s.get("rec", 0) or s.get("rec_tgt", 0):
        lines.append(f"Receiving: {s.get('rec', 0):.0f} rec / {s.get('rec_tgt', 0):.0f} tgt  {s.get('rec_yd', 0):.0f} yds  {int(s.get('rec_td', 0))} TD")

    # Recent weeks via GQL
    import asyncio
    recent = [w for w in [cur_week - 1, cur_week - 2] if w >= 1]
    if recent:
        lines.append("")
        recent_maps = await asyncio.gather(
            *[client.get_week_player_stats(w, season) for w in recent],
            return_exceptions=True,
        )
        for w, wmap in zip(recent, recent_maps):
            if isinstance(wmap, Exception):
                continue
            ws = wmap.get(str(pid)) or {}
            if ws.get("gp"):
                lines.append(f"Week {w}: {ws.get('pts_ppr', 0):.2f} PPR")

    return "\n".join(lines)


@mcp.tool()
async def get_my_lineup_outlook(week: Optional[int] = None) -> str:
    """
    Your starters for a given week with season pts/game average and injury status.
    Shows cumulative total and flags anyone who's questionable or out.
    """
    ctx = await _get_context()
    cur_week, season = await _current_week()
    if week is None:
        week = cur_week

    roster_map, matchups, players, season_map = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_matchups(ctx["league_id"], week),
        client.get_players(),
        client.get_season_stats_map(season),
    )

    my = next((r for r in roster_map.values() if r["owner_id"] == ctx["user_id"]), None)
    if not my:
        return "Couldn't find your roster."

    my_m = next((m for m in matchups if m["roster_id"] == my["roster_id"]), None)
    starters = (my_m.get("starters") or []) if my_m else my["starters"]

    lines = [f"Lineup Outlook — {my['team_name']}  Week {week}", "=" * 44]

    for pid in starters:
        if not pid:
            lines.append("  [EMPTY SLOT]")
            continue
        p = players.get(str(pid), {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        injury = p.get("injury_status", "")
        inj_s = f" [{injury}]" if injury else ""
        s = season_map.get(str(pid)) or {}
        gp = int(s.get("gp", 0) or 0)
        ppg = (s.get("pts_ppr", 0) or 0) / gp if gp else 0
        ppg_s = f"  {ppg:.2f}/g" if gp else "  — (no games)"
        lines.append(f"  {name} ({pos}, {team}){inj_s}{ppg_s}")

    return "\n".join(lines)


# ── Scoring leaders ───────────────────────────────────────────────────────────


@mcp.tool()
async def get_scoring_leaders(
    position: Optional[str] = None,
    week: Optional[int] = None,
    top: int = 15,
) -> str:
    """
    Top fantasy scorers (PPR) for the season or a specific week.
    position: QB | RB | WR | TE | K | DEF  (omit for all skill positions)
    week: specific week number, or omit for season totals
    """
    cur_week, season = await _current_week()
    players = await client.get_players()

    if week:
        # Use GQL weekly stats (REST weekly endpoint is empty for 2026)
        week_map = await client.get_week_player_stats(week, season)
        scored = [
            (pid, s, players.get(str(pid), {}))
            for pid, s in week_map.items()
            if (s.get("pts_ppr") or 0) > 0
        ]
        if position:
            scored = [(pid, s, p) for pid, s, p in scored if p.get("position", "") == position.upper()]
        scored.sort(key=lambda x: x[1].get("pts_ppr", 0), reverse=True)
        label = f"Week {week} Scoring Leaders"
    else:
        # Season totals via REST (works for 2026)
        season_raw = await client.get_season_stats(season, order_by="pts_ppr")
        season_map = {str(k): v for k, v in season_raw.items()} if isinstance(season_raw, dict) else {}
        scored = [
            (pid, s, players.get(str(pid), {}))
            for pid, s in season_map.items()
            if (s.get("pts_ppr") or 0) > 0
        ]
        if position:
            scored = [(pid, s, p) for pid, s, p in scored if p.get("position", "") == position.upper()]
        scored.sort(key=lambda x: x[1].get("pts_ppr", 0), reverse=True)
        label = f"{season} Season Scoring Leaders"

    if position:
        label += f" — {position.upper()}"

    lines = [label, "=" * 50]
    for i, (pid, s, p) in enumerate(scored[:top], 1):
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
        pos = p.get("position", "?")
        team = p.get("team") or "?"
        pts = s.get("pts_ppr", 0) or 0
        gp = int(s.get("gp", 1) or 1)
        ppg = pts / gp if gp else 0
        ppg_s = f"  ({ppg:.1f}/g)" if not week else ""
        lines.append(f"  {i:2}. {name} ({pos}, {team})  {pts:.2f}{ppg_s}")

    return "\n".join(lines)


# ── Start / Sit ───────────────────────────────────────────────────────────────


@mcp.tool()
async def get_start_sit(player1: str, player2: str, week: Optional[int] = None) -> str:
    """
    Compare two players for a start/sit decision.
    Shows season pts/game average, last 2 weeks' actual scores, and injury status.
    """
    import asyncio

    cur_week, season = await _current_week()
    if week is None:
        week = cur_week

    players, season_map = await _gather(
        client.get_players(),
        client.get_season_stats_map(season),
    )

    r1 = _find_player(player1, players)
    r2 = _find_player(player2, players)
    if not r1:
        return f"Player '{player1}' not found."
    if not r2:
        return f"Player '{player2}' not found."

    recent_weeks = [w for w in [week - 1, week - 2] if w >= 1]
    recent_maps = await asyncio.gather(
        *[client.get_week_player_stats(w, season) for w in recent_weeks],
        return_exceptions=True,
    )

    lines = [f"START / SIT — Week {week}", "=" * 44]
    ppg_scores = {}

    for label, (pid, p) in [("OPTION A", r1), ("OPTION B", r2)]:
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        injury = p.get("injury_status", "")

        s = season_map.get(str(pid)) or {}
        gp = int(s.get("gp", 0) or 0)
        ppg = (s.get("pts_ppr", 0) or 0) / gp if gp else 0
        ppg_scores[pid] = ppg

        lines += [f"\n{label}: {name} ({pos}, {team})", f"  Season avg: {ppg:.2f} PPR/game ({gp} games)"]
        if injury:
            lines.append(f"  ⚠ Status: {injury}")

        for w, wmap in zip(recent_weeks, recent_maps):
            if isinstance(wmap, Exception):
                continue
            ws = wmap.get(str(pid)) or {}
            if ws.get("gp"):
                lines.append(f"  Week {w}: {ws.get('pts_ppr', 0):.2f} PPR")

    p1_ppg = ppg_scores.get(r1[0], 0)
    p2_ppg = ppg_scores.get(r2[0], 0)
    p1_name = f"{r1[1].get('first_name', '')} {r1[1].get('last_name', '')}".strip()
    p2_name = f"{r2[1].get('first_name', '')} {r2[1].get('last_name', '')}".strip()
    better = p1_name if p1_ppg >= p2_ppg else p2_name
    diff = abs(p1_ppg - p2_ppg)

    lines += ["", f"SEASON AVG EDGE: {better} (+{diff:.2f}/g)", "Close call — weight matchup and recent news." if diff < 2 else ""]
    return "\n".join(lines)


# ── Free agents by projection ─────────────────────────────────────────────────


@mcp.tool()
async def get_free_agents_by_stats(position: Optional[str] = None) -> str:
    """
    Available free agents ranked by season PPR points per game.
    Better than trending — shows who's actually been scoring, not just who's popular.
    position: QB | RB | WR | TE | K | DEF  (omit for all)
    """
    ctx = await _get_context()
    cur_week, season = await _current_week()

    rosters, players, season_map = await _gather(
        client.get_rosters(ctx["league_id"]),
        client.get_players(),
        client.get_season_stats_map(season),
    )

    rostered: set = set()
    for r in rosters:
        rostered.update(r.get("players") or [])
        rostered.update(r.get("reserve") or [])

    positions = [position.upper()] if position else ["QB", "RB", "WR", "TE", "K", "DEF"]
    label = f"FREE AGENTS BY SEASON SCORING" + (f" — {position.upper()}" if position else "")
    lines = [label, "=" * 50]

    for pos in positions:
        available = []
        for pid, p in players.items():
            if pid in rostered:
                continue
            if (p.get("position") or "") != pos:
                continue
            if not p.get("team"):
                continue
            s = season_map.get(str(pid)) or {}
            gp = int(s.get("gp", 0) or 0)
            ppg = (s.get("pts_ppr", 0) or 0) / gp if gp else 0
            available.append((pid, p, ppg, gp))

        available.sort(key=lambda x: x[2], reverse=True)
        top = [x for x in available if x[2] > 0][:10]
        if not top:
            continue

        lines.append(f"\n{pos}:")
        for pid, p, ppg, gp in top:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            team = p.get("team", "?")
            injury = p.get("injury_status", "")
            inj_s = f" [{injury}]" if injury else ""
            lines.append(f"  {name} ({team}){inj_s}  {ppg:.2f}/g ({gp}gp)")

    return "\n".join(lines)


# ── Waiver wire activity ───────────────────────────────────────────────────────


@mcp.tool()
async def get_waiver_wire_activity(hours: int = 24) -> str:
    """
    Who's being added and dropped across all Sleeper leagues right now.
    Shows both sides together so you can spot opportunity (drops) and consensus (adds).
    """
    adds_raw, drops_raw, players = await _gather(
        client.get_trending(type_="add", hours=hours, limit=25),
        client.get_trending(type_="drop", hours=hours, limit=25),
        client.get_players(),
    )

    def fmt(item, type_):
        pid = item.get("player_id", "")
        count = item.get("count", 0)
        p = players.get(str(pid), {})
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(pid)
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        injury = p.get("injury_status", "")
        inj_s = f" [{injury}]" if injury else ""
        return f"  {name} ({pos}, {team}){inj_s}  —  {count:,} {type_}s"

    lines = [
        f"WAIVER WIRE ACTIVITY — last {hours}h",
        "=" * 50,
        "",
        "TRENDING ADDS (pick up before everyone else does):",
        *[fmt(x, "add") for x in adds_raw],
        "",
        "TRENDING DROPS (check if they're a buy-low target):",
        *[fmt(x, "drop") for x in drops_raw],
    ]
    return "\n".join(lines)


# ── All-play standings ────────────────────────────────────────────────────────


@mcp.tool()
async def get_all_play_standings() -> str:
    """
    All-play standings: each team's record if they played every opponent every week.
    Shows who's been lucky (good record vs weak schedule) vs unlucky (high scorer, bad luck).
    """
    import asyncio

    ctx = await _get_context()
    cur_week, _ = await _current_week()
    roster_map = await _build_roster_map(ctx["league_id"])

    completed_weeks = list(range(1, cur_week))
    if not completed_weeks:
        return "No completed weeks yet."

    all_matchups = await asyncio.gather(
        *[client.get_matchups(ctx["league_id"], w) for w in completed_weeks],
        return_exceptions=True,
    )

    ap: dict = {rid: {"w": 0, "l": 0} for rid in roster_map}

    for matchups in all_matchups:
        if isinstance(matchups, Exception):
            continue
        week_scores = {m["roster_id"]: (m.get("points") or 0) for m in matchups}
        for rid, pts in week_scores.items():
            if rid not in ap:
                continue
            for opp_rid, opp_pts in week_scores.items():
                if opp_rid == rid:
                    continue
                if pts > opp_pts:
                    ap[rid]["w"] += 1
                elif pts < opp_pts:
                    ap[rid]["l"] += 1

    standings = []
    for rid, rec in ap.items():
        t = roster_map.get(rid, {})
        total_ap = rec["w"] + rec["l"]
        win_rate = rec["w"] / total_ap if total_ap else 0
        actual_games = t["wins"] + t["losses"]
        expected_wins = round(win_rate * actual_games)
        luck = t["wins"] - expected_wins
        standings.append((rid, t, rec, luck))

    standings.sort(key=lambda x: (x[2]["w"], x[1].get("fpts", 0)), reverse=True)

    lines = [
        "ALL-PLAY STANDINGS  (if you played every team each week)",
        "=" * 56,
    ]
    for rid, t, rec, luck in standings:
        actual = f"{t['wins']}-{t['losses']}"
        ap_rec = f"{rec['w']}-{rec['l']}"
        luck_s = f"  lucky +{luck}" if luck > 0 else (f"  unlucky {luck}" if luck < 0 else "")
        lines.append(f"  {t.get('team_name', '?'):<26} AP: {ap_rec:<8} Real: {actual}{luck_s}")

    return "\n".join(lines)


# ── Power rankings ─────────────────────────────────────────────────────────────


@mcp.tool()
async def get_power_rankings() -> str:
    """
    Power rankings based on recent scoring (last 3 weeks, most recent weighted highest).
    Shows who's hot vs cooling off regardless of their actual record.
    """
    import asyncio

    ctx = await _get_context()
    cur_week, _ = await _current_week()
    roster_map = await _build_roster_map(ctx["league_id"])

    last = cur_week - 1
    week_weights = [(last, 3), (last - 1, 2), (last - 2, 1)]
    weeks_to_fetch = [(w, wt) for w, wt in week_weights if w >= 1]

    if not weeks_to_fetch:
        return "Not enough completed weeks for power rankings."

    results = await asyncio.gather(
        *[client.get_matchups(ctx["league_id"], w) for w, _ in weeks_to_fetch],
        return_exceptions=True,
    )

    weighted: dict = {rid: 0.0 for rid in roster_map}
    total_wt: dict = {rid: 0 for rid in roster_map}

    for (w, wt), matchups in zip(weeks_to_fetch, results):
        if isinstance(matchups, Exception):
            continue
        for m in matchups:
            rid = m.get("roster_id")
            if rid in weighted:
                weighted[rid] += (m.get("points") or 0) * wt
                total_wt[rid] += wt

    rankings = []
    for rid, t in roster_map.items():
        score = weighted[rid] / total_wt[rid] if total_wt[rid] else 0
        standings_rank = sorted(
            roster_map.values(), key=lambda r: (r["wins"], r["fpts"]), reverse=True
        ).index(t) + 1
        rankings.append((rid, t, score, standings_rank))

    rankings.sort(key=lambda x: x[2], reverse=True)

    lines = [
        "POWER RANKINGS  (weighted: last wk ×3, 2 wks ×2, 3 wks ×1)",
        "=" * 56,
    ]
    for pr_rank, (rid, t, score, stand_rank) in enumerate(rankings, 1):
        rec = f"{t['wins']}-{t['losses']}"
        move = stand_rank - pr_rank
        move_s = f"  ↑{move}" if move > 0 else (f"  ↓{abs(move)}" if move < 0 else "  →")
        lines.append(f"  {pr_rank:2}. {t.get('team_name', '?'):<26} {rec:<6} {score:.1f} pts{move_s} vs standings")

    return "\n".join(lines)


# ── Injury report ─────────────────────────────────────────────────────────────


@mcp.tool()
async def get_injury_report() -> str:
    """
    All rostered players currently listed as Q / D / O / IR / SUS, grouped by NFL team.
    Run this before setting your lineup.
    """
    ctx = await _get_context()
    rosters, players = await _gather(
        client.get_rosters(ctx["league_id"]),
        client.get_players(),
    )

    all_rostered: set = set()
    for r in rosters:
        all_rostered.update(r.get("players") or [])
        all_rostered.update(r.get("reserve") or [])

    STATUS_RANK = {"O": 0, "D": 1, "Q": 2, "IR": 3, "PUP": 4, "SUS": 5}

    by_team: dict = {}
    for pid, p in players.items():
        if pid not in all_rostered:
            continue
        injury = p.get("injury_status", "")
        if not injury:
            continue
        team = p.get("team") or "FA"
        by_team.setdefault(team, []).append((pid, p, injury))

    for team in by_team:
        by_team[team].sort(key=lambda x: STATUS_RANK.get(x[2], 9))

    lines = ["INJURY REPORT — Rostered Players Only", "=" * 44]

    if not by_team:
        lines.append("No injuries flagged for rostered players.")
        return "\n".join(lines)

    for team in sorted(by_team.keys()):
        lines.append(f"\n{team}:")
        for pid, p, injury in by_team[team]:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            pos = p.get("position", "?")
            notes = p.get("injury_notes", "")
            notes_s = f" — {notes}" if notes else ""
            lines.append(f"  [{injury}] {name} ({pos}){notes_s}")

    return "\n".join(lines)


# ── DST streamers ─────────────────────────────────────────────────────────────


@mcp.tool()
async def get_dst_streamers(week: Optional[int] = None) -> str:
    """
    Best available DSTs to stream this week.
    Shows season stats (pts/g, sacks, pts allowed) plus the latest news on each
    defense — news articles include upcoming opponent context.
    """
    import asyncio

    ctx = await _get_context()
    cur_week, season = await _current_week()
    if week is None:
        week = cur_week

    rosters, players, season_stats_raw = await _gather(
        client.get_rosters(ctx["league_id"]),
        client.get_players(),
        client.get_season_stats(season, positions=["DEF"]),
    )

    # Season stats endpoint returns {player_id: stats_dict}
    stats_map = {str(k): v for k, v in season_stats_raw.items()} if isinstance(season_stats_raw, dict) else {}

    rostered: set = set()
    for r in rosters:
        rostered.update(r.get("players") or [])

    available = []
    for pid, p in players.items():
        if pid in rostered:
            continue
        if p.get("position") != "DEF":
            continue
        if not p.get("team"):
            continue
        stats = stats_map.get(str(pid)) or {}
        gp = int(stats.get("gp", 0) or 0)
        ppg = (stats.get("pts_ppr", 0) or 0) / gp if gp else 0
        available.append((pid, p, ppg, stats))

    available.sort(key=lambda x: x[2], reverse=True)
    top = available[:8]

    if not top:
        return "No DSTs available on waivers."

    # Fetch news for each DST in parallel (news mentions upcoming opponent)
    news_list = await asyncio.gather(
        *[client.get_player_news_gql(pid, limit=1) for pid, *_ in top],
        return_exceptions=True,
    )

    lines = [f"DST STREAMERS — Week {week}", "=" * 50]

    for (pid, p, ppg, stats), news in zip(top, news_list):
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or p.get("team", "?")
        team = p.get("team", "?")
        gp = int(stats.get("gp", 0) or 0)
        sacks = stats.get("sack", 0) or 0
        pts_allow = stats.get("pts_allow", 0) or 0

        lines.append(f"\n{name} ({team})")
        lines.append(f"  Season: {ppg:.1f} PPR/g  |  {sacks:.0f} sacks  |  {pts_allow:.0f} pts allowed  ({gp}gp)")

        if isinstance(news, list) and news:
            meta = (news[0].get("metadata") or {})
            analysis = meta.get("analysis") or meta.get("description") or ""
            if analysis:
                lines.append(f"  {analysis[:180]}")

    return "\n".join(lines)


# ── Playoff bracket ───────────────────────────────────────────────────────────


@mcp.tool()
async def get_playoff_bracket() -> str:
    """
    Current playoff bracket with results and upcoming matchups.
    Only shows useful data once playoffs have started.
    """
    ctx = await _get_context()
    roster_map, bracket, league = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_winners_bracket(ctx["league_id"]),
        client.get_league(ctx["league_id"]),
    )

    settings = league.get("settings") or {}
    playoff_start = settings.get("playoff_week_start", 15)
    cur_week, _ = await _current_week()

    if cur_week < playoff_start:
        return f"Playoffs start Week {playoff_start} — {playoff_start - cur_week} weeks away."

    if not bracket:
        return "No playoff bracket data available yet."

    def team_name(rid):
        if rid is None:
            return "TBD"
        return roster_map.get(rid, {}).get("team_name", f"Team {rid}")

    by_round: dict = {}
    for match in bracket:
        r = match.get("r", 0)
        by_round.setdefault(r, []).append(match)

    lines = ["PLAYOFF BRACKET", "=" * 44]
    for r in sorted(by_round.keys()):
        round_week = playoff_start + r - 1
        lines.append(f"\nRound {r}  (Week {round_week}):")
        for m in by_round[r]:
            t1 = team_name(m.get("t1"))
            t2 = team_name(m.get("t2"))
            winner = m.get("w")
            if winner:
                lines.append(f"  {team_name(winner)} def. {team_name(m.get('l'))}")
            else:
                lines.append(f"  {t1} vs {t2}")

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────


async def _warmup() -> None:
    """Pre-load player DB and probe GQL so the circuit breaker opens before any user request."""
    import asyncio
    await asyncio.gather(
        client.get_players(),
        client.get_nfl_state(),
        client.graphql("{ __typename }"),  # probe — opens circuit breaker if blocked
        return_exceptions=True,
    )


def main():
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "http":
        import asyncio
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        # Pre-load player DB before serving requests
        asyncio.run(_warmup())

        # Add /health endpoint so Railway's health check returns 200
        async def _health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok", "service": "sleeper-mcp"})

        mcp._get_additional_http_routes = lambda: [Route("/health", _health)]

        port = int(os.getenv("PORT", "8000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
