import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

CACHE_DIR = Path.home() / ".cache" / "sleeper-mcp"
PLAYERS_CACHE = CACHE_DIR / "players.json"
PLAYERS_CACHE_TTL = 86400  # 24 hours

BASE_URL = "https://api.sleeper.app/v1"


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
