import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

CACHE_DIR = Path.home() / ".cache" / "sleeper-mcp"
PLAYERS_CACHE = CACHE_DIR / "players.json"
PLAYERS_CACHE_TTL = 86400  # 24 hours

BASE_URL = "https://api.sleeper.app/v1"
GQL_URL = "https://api.sleeper.app/graphql"

# Week stats cache: {"{season}_{week}": {player_id: stats_dict}}
_week_stats_cache: dict = {}

# GQL circuit breaker: if GQL fails, skip it for _GQL_RETRY_DELAY seconds
_gql_failure_time: float = 0.0
_GQL_RETRY_DELAY: float = 300.0  # 5 minutes


class SleeperClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token
        self._players: dict = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _headers(self, auth: bool = False) -> dict:
        h = {"Accept": "application/json"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def get(self, path: str, params: Optional[dict] = None, auth: bool = False) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
            url = f"{BASE_URL}{path}"
            resp = await http.get(url, params=params, headers=self._headers(auth))
            resp.raise_for_status()
            return resp.json()

    # ── Player cache ──────────────────────────────────────────────────────────

    async def get_players(self) -> dict:
        if self._players:
            return self._players
        if PLAYERS_CACHE.exists():
            age = time.time() - PLAYERS_CACHE.stat().st_mtime
            if age < PLAYERS_CACHE_TTL:
                self._players = json.loads(PLAYERS_CACHE.read_text())
                return self._players
        data = await self.get("/players/nfl")
        PLAYERS_CACHE.write_text(json.dumps(data))
        self._players = data
        return data

    def resolve_player(self, pid: str, players: dict) -> str:
        p = players.get(str(pid), {})
        if not p:
            return str(pid)
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        pos = p.get("position", "?")
        team = p.get("team") or "FA"
        injury = p.get("injury_status", "")
        injury_tag = f" [{injury}]" if injury else ""
        return f"{name} ({pos}, {team}){injury_tag}"

    # ── NFL state ─────────────────────────────────────────────────────────────

    async def get_nfl_state(self) -> dict:
        return await self.get("/state/nfl")

    # ── User ──────────────────────────────────────────────────────────────────

    async def get_user(self, username_or_id: str) -> dict:
        return await self.get(f"/user/{username_or_id}")

    async def get_leagues(self, user_id: str, season: str = "2026") -> list:
        return await self.get(f"/user/{user_id}/leagues/nfl/{season}")

    # ── League ────────────────────────────────────────────────────────────────

    async def get_league(self, league_id: str) -> dict:
        return await self.get(f"/league/{league_id}")

    async def get_rosters(self, league_id: str) -> list:
        return await self.get(f"/league/{league_id}/rosters")

    async def get_users(self, league_id: str) -> list:
        return await self.get(f"/league/{league_id}/users")

    async def get_matchups(self, league_id: str, week: int) -> list:
        return await self.get(f"/league/{league_id}/matchups/{week}")

    async def get_transactions(self, league_id: str, week: int) -> list:
        return await self.get(f"/league/{league_id}/transactions/{week}")

    async def get_traded_picks(self, league_id: str) -> list:
        return await self.get(f"/league/{league_id}/traded_picks")

    async def get_winners_bracket(self, league_id: str) -> list:
        return await self.get(f"/league/{league_id}/winners_bracket")

    async def get_league_drafts(self, league_id: str) -> list:
        return await self.get(f"/league/{league_id}/drafts")

    async def get_draft_picks(self, draft_id: str) -> list:
        return await self.get(f"/draft/{draft_id}/picks")

    # ── Players ───────────────────────────────────────────────────────────────

    async def get_trending(self, type_: str = "add", hours: int = 24, limit: int = 25) -> list:
        return await self.get(
            f"/players/nfl/trending/{type_}",
            params={"lookback_hours": hours, "limit": limit},
        )

    # ── Stats (undocumented public endpoints) ─────────────────────────────────

    async def get_player_stats(
        self,
        week: int,
        season: str = "2026",
        season_type: str = "regular",
        positions: Optional[list] = None,
    ) -> list:
        params: dict = {"season_type": season_type, "season": season}
        if positions:
            params["position[]"] = positions
        return await self.get(f"/stats/nfl/player/{week}", params=params)

    async def get_season_stats(
        self,
        season: str = "2026",
        season_type: str = "regular",
        positions: Optional[list] = None,
        order_by: str = "pts_ppr",
    ) -> list:
        params: dict = {"order_by": order_by}
        if positions:
            params["position[]"] = positions
        return await self.get(f"/stats/nfl/{season_type}/{season}", params=params)

    async def get_player_projections(
        self,
        week: int,
        season: str = "2026",
        season_type: str = "regular",
        positions: Optional[list] = None,
    ) -> list:
        params: dict = {"season_type": season_type, "season": season}
        if positions:
            params["position[]"] = positions
        return await self.get(f"/projections/nfl/player/{week}", params=params)

    # ── REST stats map (public, no auth needed) ───────────────────────────────

    async def get_player_stats_map(self, week: int, season: str = "2026") -> dict:
        """REST-only weekly stats map. Returns empty dicts for 2026 but avoids GQL recursion."""
        try:
            raw = await self.get_player_stats(week, season)
            if isinstance(raw, dict):
                return {str(k): v for k, v in raw.items() if v}
            return {}
        except Exception:
            return {}

    async def get_season_stats_map(self, season: str = "2026") -> dict:
        """Returns {player_id: stats_dict} for season totals via REST."""
        cache_key = f"season_{season}"
        if cache_key in _week_stats_cache:
            return _week_stats_cache[cache_key]
        raw = await self.get_season_stats(season)
        # REST season stats endpoint already returns {player_id: stats_dict}
        result = {str(k): v for k, v in raw.items()} if isinstance(raw, dict) else {}
        _week_stats_cache[cache_key] = result
        return result

    # ── GraphQL (internal Sleeper API) ────────────────────────────────────────

    async def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        global _gql_failure_time
        now = time.time()
        if _gql_failure_time and (now - _gql_failure_time) < _GQL_RETRY_DELAY:
            raise Exception("GQL circuit breaker open — skipping to REST fallback")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://sleeper.com",
            "Referer": "https://sleeper.com/",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(GQL_URL, json=payload, headers=headers)
                resp.raise_for_status()
                _gql_failure_time = 0.0  # reset on success
                return resp.json()
        except Exception:
            _gql_failure_time = time.time()  # open circuit breaker
            raise

    async def get_week_player_stats(self, week: int, season: str = "2026") -> dict:
        """
        Returns {player_id: stats_dict} for all players in a week.
        Tries GQL first; falls back to REST on auth failure.
        """
        cache_key = f"{season}_{week}"
        if cache_key in _week_stats_cache:
            return _week_stats_cache[cache_key]

        try:
            result = await self.graphql("""
            query WeeklyStats($season: String!, $week: Int!, $season_type: String!) {
              weekly_stats(
                sport: "nfl"
                season: $season
                season_type: $season_type
                week: $week
                category: "stat"
                order_by: "pts_ppr"
              ) {
                player_id
                stats
                week
              }
            }
            """, {"season": season, "week": week, "season_type": "regular"})

            data = result.get("data") or {}
            rows = data.get("weekly_stats") or []
            by_player = {row["player_id"]: row.get("stats") or {} for row in rows}
        except Exception:
            by_player = await self.get_player_stats_map(week, season)

        _week_stats_cache[cache_key] = by_player
        return by_player

    async def get_stats_for_players(
        self,
        player_ids: list,
        week: int,
        season: str = "2026",
    ) -> dict:
        """
        Returns {player_id: stats_dict} for specific players in a week.
        Tries GQL first; falls back to REST on auth failure.
        """
        try:
            ids_gql = json.dumps(player_ids)
            result = await self.graphql(f"""
            query {{
              stats_for_players_in_week(
                player_ids: {ids_gql}
                week: {week}
                season: "{season}"
                season_type: "regular"
                sport: "nfl"
                category: "stat"
              ) {{
                player_id
                stats
              }}
            }}
            """)
            data = result.get("data") or {}
            rows = data.get("stats_for_players_in_week") or []
            return {row["player_id"]: row.get("stats") or {} for row in rows}
        except Exception:
            # Fall back to full week GQL stats and filter to requested players
            all_stats = await self.get_week_player_stats(week, season)
            return {pid: all_stats[pid] for pid in [str(p) for p in player_ids] if pid in all_stats}

    async def get_projections_map(self, week: int, season: str = "2026") -> dict:
        """Returns {player_id: proj_dict} for all players this week. Cached."""
        cache_key = f"proj_{season}_{week}"
        if cache_key in _week_stats_cache:
            return _week_stats_cache[cache_key]
        proj_list = await self.get_player_projections(week, season)
        result = {str(s["player_id"]): s for s in proj_list if "player_id" in s}
        _week_stats_cache[cache_key] = result
        return result

    async def get_player_news_gql(self, player_id: str, limit: int = 5) -> list:
        """Recent news/game recaps for a player via GQL."""
        try:
            result = await self.graphql("""
            query GetNews($pid: String!) {
              get_player_news(player_id: $pid, sport: "nfl") {
                player_id
                published
                source
                metadata
              }
            }
            """, {"pid": player_id})
            items = (result.get("data") or {}).get("get_player_news") or []
            return items[:limit]
        except Exception:
            return []

    async def get_player_outlook_gql(self, player_id: str, season: str = "2026") -> dict:
        """Season-long analyst outlook for a player via GQL."""
        try:
            result = await self.graphql("""
            query GetOutlook($pid: String!, $season: String!) {
              get_player_outlook(player_id: $pid, sport: "nfl", season: $season) {
                player_id
                published
                source
                metadata
              }
            }
            """, {"pid": player_id, "season": season})
            return (result.get("data") or {}).get("get_player_outlook") or {}
        except Exception:
            return {}

    async def get_matchup_player_map(self, league_id: str, week: int) -> dict:
        """
        Returns {roster_id: {player_id: fantasy_pts}} using Sleeper's internal
        scoring engine (exact pts, includes bonus scoring).
        Requires SLEEPER_TOKEN — falls back to None if unauthorized.
        """
        if not self.token:
            return {}
        try:
            result = await self.graphql("""
            query MatchupLegs($league_id: String!, $round: Int!) {
              matchup_legs(league_id: $league_id, round: $round) {
                roster_id
                matchup_id
                points
                proj_points
                starters
                player_map
              }
            }
            """, {"league_id": league_id, "round": week})
            data = result.get("data") or {}
            legs = data.get("matchup_legs") or []
            return {leg["roster_id"]: leg.get("player_map") or {} for leg in legs}
        except Exception:
            return {}

    def scoring_type(self, scoring_settings: dict) -> str:
        rec = scoring_settings.get("rec", 0)
        if rec >= 1.0:
            return "pts_ppr"
        elif rec >= 0.5:
            return "pts_half_ppr"
        return "pts_std"
