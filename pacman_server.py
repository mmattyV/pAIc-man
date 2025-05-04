"""
pAIcMan Server - Implements the gRPC service defined in pacman.proto
"""
import grpc
import uuid
import time
import threading
import logging
import argparse
import concurrent.futures
import sys
import os
from typing import Dict, List, Optional

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"logs/server.log", mode='a')
    ]
)
logger = logging.getLogger("pacman-server")

class GameSession:
    """Represents an active game session on the server"""
    def __init__(self, game_id: str, layout_name: str, max_players: int):
        self.game_id = game_id
        self.layout_name = layout_name
        self.max_players = max_players
        self.current_players = 0
        self.player_streams = {}  # player_id -> stream
        self.status = pacman_pb2.WAITING
        self.lock = threading.RLock()
        # Add more game state as needed

    def add_player(self, player_id: str, stream):
        """Add a player to this game session"""
        with self.lock:
            if self.current_players >= self.max_players:
                return False
            self.player_streams[player_id] = stream
            self.current_players += 1
            if self.current_players == self.max_players:
                self.status = pacman_pb2.IN_PROGRESS
            return True

    def remove_player(self, player_id: str):
        """Remove a player from this game session"""
        with self.lock:
            if player_id in self.player_streams:
                del self.player_streams[player_id]
                self.current_players -= 1
                if self.current_players == 0:
                    self.status = pacman_pb2.FINISHED
                return True
            return False

    def broadcast_state(self, game_state: pacman_pb2.GameState):
        """Broadcast game state to all connected players"""
        with self.lock:
            dead_streams = []
            for player_id, stream in self.player_streams.items():
                try:
                    stream.put_nowait(game_state)  # Non-blocking put
                except:
                    logger.warning(f"Failed to send state to player {player_id}")
                    dead_streams.append(player_id)

            # Remove dead streams
            for player_id in dead_streams:
                self.remove_player(player_id)

    def get_info(self) -> pacman_pb2.GameInfo:
        """Return information about this game session"""
        with self.lock:
            return pacman_pb2.GameInfo(
                game_id=self.game_id,
                layout_name=self.layout_name,
                current_players=self.current_players,
                max_players=self.max_players,
                status=self.status
            )

class PacmanServicer(pacman_pb2_grpc.PacmanGameServicer):
    """Implementation of the PacmanGame service"""

    def __init__(self, port: int, data_dir: str):
        self.port = port
        self.data_dir = data_dir
        self.games: Dict[str, GameSession] = {}
        self.games_lock = threading.RLock()
        logger.info(f"PacmanServicer initialized on port {port} with data directory {data_dir}")

        # Create a game tick thread for updates
        self.running = True
        self.tick_thread = threading.Thread(target=self._game_tick_loop)
        self.tick_thread.daemon = True
        self.tick_thread.start()
        logger.info("Started game tick loop")

    def _game_tick_loop(self):
        """Main game loop that processes updates at 60Hz"""
        while self.running:
            try:
                tick_start = time.time()

                # Process all active games
                with self.games_lock:
                    for game_id, game in list(self.games.items()):
                        if game.status == pacman_pb2.IN_PROGRESS:
                            # Update game state (simplified for now)
                            game_state = self._process_game_update(game)
                            # Broadcast to all players
                            game.broadcast_state(game_state)

                # Sleep to maintain 60Hz
                elapsed = time.time() - tick_start
                sleep_time = max(0, (1/60) - elapsed)
                time.sleep(sleep_time)
            except Exception as e:
                logger.error(f"Error in game tick loop: {e}", exc_info=True)

    def _process_game_update(self, game: GameSession) -> pacman_pb2.GameState:
        """Process a single game update (would include game logic)"""
        # This is a simplified placeholder - would be replaced with actual game logic
        # In the full implementation, this would use the helpers.game module
        return pacman_pb2.GameState(
            game_id=game.game_id,
            agents=[],  # Would populate with actual agent states
            food=[],    # Would populate with food positions
            capsules=[],
            walls=[],
            score=0,
            status=game.status
        )

    def CreateGame(self, request, context):
        """RPC method to create a new game"""
        try:
            game_id = str(uuid.uuid4())
            layout_name = request.layout_name
            max_players = request.max_players

            # Validate request
            if max_players <= 0 or max_players > 4:
                return pacman_pb2.GameSession(
                    game_id="",
                    success=False,
                    error_message="Invalid max_players value. Must be between 1 and 4."
                )

            # Create new game session
            new_game = GameSession(game_id, layout_name, max_players)

            with self.games_lock:
                self.games[game_id] = new_game

            logger.info(f"Created new game: {game_id}, layout: {layout_name}, max players: {max_players}")

            return pacman_pb2.GameSession(
                game_id=game_id,
                success=True,
                error_message=""
            )
        except Exception as e:
            logger.error(f"Error creating game: {e}", exc_info=True)
            return pacman_pb2.GameSession(
                game_id="",
                success=False,
                error_message=f"Server error: {str(e)}"
            )

    def ListGames(self, request, context):
        """RPC method to list available games"""
        try:
            games_list = []

            with self.games_lock:
                for game_id, game in self.games.items():
                    # Only include games that aren't full or finished
                    if game.status != pacman_pb2.FINISHED:
                        games_list.append(game.get_info())

            return pacman_pb2.GamesList(games=games_list)
        except Exception as e:
            logger.error(f"Error listing games: {e}", exc_info=True)
            return pacman_pb2.GamesList(games=[])

    def PlayGame(self, request_iterator, context):
        """RPC method for bidirectional streaming gameplay"""
        player_id = None
        game_id = None
        game = None

        # Queue for game state updates to this player
        from queue import Queue
        state_queue = Queue()

        try:
            # Process the first message which should be a JOIN action
            first_action = next(request_iterator)

            if first_action.action_type != pacman_pb2.JOIN:
                yield pacman_pb2.GameState(
                    game_id="",
                    status=pacman_pb2.FINISHED,
                    winner_id=""
                )
                return

            player_id = first_action.player_id
            game_id = first_action.game_id

            # Validate game exists
            with self.games_lock:
                if game_id not in self.games:
                    yield pacman_pb2.GameState(
                        game_id=game_id,
                        status=pacman_pb2.FINISHED,
                        winner_id=""
                    )
                    return

                game = self.games[game_id]

                # Add player to game
                if not game.add_player(player_id, state_queue):
                    yield pacman_pb2.GameState(
                        game_id=game_id,
                        status=pacman_pb2.FINISHED,
                        winner_id=""
                    )
                    return

            logger.info(f"Player {player_id} joined game {game_id}")

            # Handle player actions in a separate thread
            def process_actions():
                try:
                    for action in request_iterator:
                        # Handle player's action (would update game state based on action)
                        if action.action_type == pacman_pb2.LEAVE:
                            logger.info(f"Player {player_id} left game {game_id}")
                            game.remove_player(player_id)
                            break
                        elif action.action_type == pacman_pb2.MOVE:
                            # Process move - in full implementation would update in game logic
                            logger.debug(f"Player {player_id} moved {action.direction} in game {game_id}")
                except Exception as e:
                    logger.error(f"Error processing player actions: {e}", exc_info=True)
                finally:
                    # Ensure player is removed when stream ends
                    if game and player_id:
                        game.remove_player(player_id)

            # Start action processing thread
            action_thread = threading.Thread(target=process_actions)
            action_thread.daemon = True
            action_thread.start()

            # Stream game states back to the client
            while context.is_active():
                try:
                    # Wait for a game state update (with timeout to check if context is still active)
                    game_state = state_queue.get(timeout=1.0)
                    yield game_state
                except:
                    # Queue.get timed out, check if context is still active
                    continue

        except Exception as e:
            logger.error(f"Error in PlayGame stream: {e}", exc_info=True)
        finally:
            # Ensure cleanup
            if game and player_id:
                game.remove_player(player_id)
            logger.info(f"PlayGame stream ended for player {player_id} in game {game_id}")

def serve(port: int, data_dir: str):
    """Start the gRPC server"""
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    # Create server
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=10))

    # Add our service to the server
    servicer = PacmanServicer(port, data_dir)
    pacman_pb2_grpc.add_PacmanGameServicer_to_server(servicer, server)

    # Start server on specified port
    server.add_insecure_port(f'[::]:{port}')
    server.start()

    logger.info(f"Server started on port {port}")

    try:
        # Keep server running
        while True:
            time.sleep(86400)  # Sleep for a day
    except KeyboardInterrupt:
        logger.info("Server stopping...")
        servicer.running = False
        server.stop(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pAIcMan Game Server")
    parser.add_argument("--port", type=int, default=50051, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default="./data", help="Directory to store data")

    args = parser.parse_args()
    serve(args.port, args.data_dir)
