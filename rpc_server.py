"""Simple HTTP server for RPC status checking."""

import asyncio
import json
import logging
from aiohttp import web
from storage.json_store import store
from storage.br_store import br_store

logger = logging.getLogger(__name__)


async def get_player_status(request: web.Request) -> web.Response:
    """Get player's tournament status for RPC."""
    try:
        discord_id = request.match_info.get('discord_id')
        if not discord_id:
            return web.json_response({"error": "Missing discord_id"}, status=400)

        discord_id = int(discord_id)

        # Check regular tournaments
        active_tournament = None
        player_info = None

        for tournament_data in store.get_all():
            tournament = store.get(tournament_data["guild_id"], tournament_data["channel_id"])
            if not tournament:
                continue

            # Check if player is in this tournament
            for player in tournament.players:
                if player["id"] == discord_id:
                    player_info = player
                    active_tournament = tournament
                    break

            if active_tournament:
                break

        # Check BR tournaments if not found in regular
        if not active_tournament:
            for br_tournament in br_store.get_all():
                for player in br_tournament.players:
                    if player["id"] == discord_id:
                        player_info = player
                        active_tournament = br_tournament
                        break

                if active_tournament:
                    break

        if not active_tournament:
            return web.json_response({"status": "none"})

        # Determine status based on tournament phase
        status = "IN_LOBBY"
        details = ""
        state = ""
        party_size = [0, 0]
        start_time = None

        try:
            from models.tournament import TournamentPhase as RegularPhase
            from models.br_tournament import BRTournamentPhase

            phase = active_tournament.phase.value if hasattr(active_tournament, 'phase') else ""

            # Check if this is a BR tournament
            is_br = hasattr(active_tournament, 'map_count')

            if is_br:
                # BR tournament
                if phase == "registration":
                    status = "IN_LOBBY"
                    details = "BR Tournament Registration"
                    state = f"Players: {len(active_tournament.players)}/48"
                    party_size = [len(active_tournament.players), 48]
                elif phase in ["bermuda", "purgatory", "kalahari"]:
                    status = "IN_GAME"
                    details = f"BR Cup - {phase.capitalize()}"
                    # Find player's team
                    for team in active_tournament.teams:
                        if player_info["name"] in team.get("players", []):
                            state = f"Team: {team.get('name', 'Unknown')}"
                            break
                    party_size = [len(active_tournament.players), len(active_tournament.players)]
                elif phase in ["results", "complete"]:
                    status = "none"
            else:
                # Regular tournament
                if phase in ["SETUP", "DRAFT"]:
                    status = "IN_LOBBY"
                    details = "Tournament Setup"
                    state = f"Phase: {phase}"
                    party_size = [len(active_tournament.players), getattr(active_tournament, 'size', 8)]
                elif phase in ["QUALIFIERS", "SEMIFINALS", "FINALS"]:
                    status = "IN_GAME"
                    details = f"{phase.capitalize()}"
                    # Find player's team by checking circles
                    circles = [
                        getattr(active_tournament, 'circle1', []),
                        getattr(active_tournament, 'circle2', []),
                        getattr(active_tournament, 'circle3', []),
                        getattr(active_tournament, 'circle4', [])
                    ]
                    for idx, circle in enumerate(circles, 1):
                        for player in circle:
                            if player.get("id") == discord_id:
                                state = f"Team {idx}"
                                break
                    party_size = [len(active_tournament.players), getattr(active_tournament, 'size', 8)]
                elif phase == "COMPLETE":
                    status = "none"
        except Exception as e:
            logger.error(f"Error determining tournament status: {e}")
            status = "none"

        response_data = {
            "status": status,
            "details": details,
            "state": state,
            "party_size": party_size,
            "large_image": "logo_rz3",
            "large_text": "r!z3 Tournament System"
        }

        if start_time:
            response_data["start_time"] = start_time

        return web.json_response(response_data)

    except Exception as e:
        logger.error(f"Error getting player status: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def health_check(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


def create_rpc_app() -> web.Application:
    """Create aiohttp application for RPC server."""
    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/status/{discord_id}', get_player_status)
    return app


async def start_rpc_server(host: str = "0.0.0.0", port: int = 8080):
    """Start the RPC HTTP server."""
    app = create_rpc_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"RPC server started on http://{host}:{port}")
    return runner
