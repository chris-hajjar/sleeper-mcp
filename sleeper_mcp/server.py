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
    Shows both teams' starters, scores, and matchup status.
    """
    ctx = await _get_context()
    cur_week, _ = await _current_week()
    if week is None:
        week = cur_week

    roster_map, matchups, players = await _gather(
        _build_roster_map(ctx["league_id"]),
        client.get_matchups(ctx["league_id"], week),
        client.get_players(),
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

    lines = [
        f"Week {week} Matchup  [{status}]",
        f"",
        f"  {my['team_name']:<28} {my_pts:>7.2f} pts",
        f"  {'vs':>28}",
        f"  {opp.get('team_name', 'Opponent'):<28} {opp_pts:>7.2f} pts",
        "",
        "YOUR STARTERS:",
        *[f"  {_fmt_player(pid, players)}" for pid in my_starters if pid],
    ]
    if opp_m:
        lines += [
            "",
            "OPPONENT STARTERS:",
            *[f"  {_fmt_player(pid, players)}" for pid in opp_starters if pid],
        ]
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


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    mcp.run()


if __name__ == "__main__":
    main()
