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
from queue import Queue, Empty

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

# Import Pacman game components
from helpers.game import Directions, Actions, AgentState, Configuration
from helpers.game import Grid
import helpers.layout as layout
from util import nearestPoint, manhattanDistance

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/server.log", mode='a')
    ]
)
logger = logging.getLogger("pacman-server")

class GameSession:
    """Represents a game session"""
    def __init__(self, game_id: str, layout_name: str, max_players: int):
        self.game_id = game_id
        self.layout_name = layout_name
        self.max_players = 2
        self.current_players = 0
        self.lock = threading.RLock()
        self.player_streams = {}
        self.player_roles = {}  # Maps player_id to role (PACMAN or GHOST)
        self.player_directions = {}  # Maps player_id to current direction
        self.player_positions = {}  # Maps player_id to current position
        self.pacman_player = None  # Track which player is Pacman
        self.status = pacman_pb2.WAITING

        # Game state variables
        self.score = 0

        # Initialize the game layout
        self.layout = layout.getLayout(layout_name)
        if not self.layout:
            logger.warning(f"Could not find layout {layout_name}, using mediumClassic as fallback")
            self.layout = layout.getLayout("mediumClassic")

        # Initialize game state including food grid
        self.initialize_game_state()

        # Player action queue
        self.player_actions = {}

        # Start game update thread
        self.running = True
        self.update_thread = threading.Thread(target=self.update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
        logger.info(f"Created new game session {game_id}")

    def initialize_game_state(self):
        """Initialize a new game state with food, capsules, etc."""
        self.food = self.layout.food.copy()
        self.capsules = self.layout.capsules[:]
        self.walls = self.layout.walls
        self.score = 0

        # Initialize starting positions
        self.player_positions = {}
        self.player_directions = {}
        self.scared_timers = {}  # Track ghost scared timers

    def add_player(self, player_id: str, stream):
        """Add a player to this game session"""
        with self.lock:
            if self.current_players >= self.max_players:
                return False

            # Assign role: first player is Pacman, rest are ghosts
            if self.pacman_player is None:
                self.player_roles[player_id] = pacman_pb2.PACMAN
                self.pacman_player = player_id
                logger.info(f"Player {player_id} joined as PACMAN")
            else:
                self.player_roles[player_id] = pacman_pb2.GHOST
                logger.info(f"Player {player_id} joined as GHOST")

            self.player_streams[player_id] = stream
            self.current_players += 1

            # Initialize player position
            self.initialize_player_position(player_id)

            # If we have at least one Pacman and one ghost, game can start
            if self.pacman_player is not None and self.current_players > 1:
                self.status = pacman_pb2.IN_PROGRESS

            return True

    def initialize_player_position(self, player_id):
        """Initialize a player's position based on their role"""
        role = self.player_roles.get(player_id)
        if role is None:
            return

        if role == pacman_pb2.PACMAN:
            # Start Pacman at the layout's Pacman start position
            for is_pacman, pos in self.layout.agentPositions:
                if is_pacman:
                    self.player_positions[player_id] = pos
                    self.player_directions[player_id] = Directions.STOP
                    break
        else:  # GHOST
            # Assign ghosts to different starting positions
            ghost_count = len([p for p, r in self.player_roles.items()
                              if r == pacman_pb2.GHOST and p in self.player_positions])

            # Get ghost starting positions from layout
            ghost_positions = [pos for is_pacman, pos in self.layout.agentPositions if not is_pacman]

            # If we have too many ghosts for layout positions, place them around the map
            if ghost_count >= len(ghost_positions):
                # Place extra ghosts in corners or other strategic locations
                extra_positions = [(1, 1), (self.walls.width-2, 1),
                                  (1, self.walls.height-2),
                                  (self.walls.width-2, self.walls.height-2)]
                # Choose a position that's not a wall
                for pos in extra_positions:
                    if not self.walls[pos[0]][pos[1]] and ghost_count < len(ghost_positions) + len(extra_positions):
                        self.player_positions[player_id] = pos
                        self.player_directions[player_id] = Directions.STOP
                        self.scared_timers[player_id] = 0
                        break
            else:
                # Use layout ghost position
                self.player_positions[player_id] = ghost_positions[ghost_count % len(ghost_positions)]
                self.player_directions[player_id] = Directions.STOP
                self.scared_timers[player_id] = 0

    def remove_player(self, player_id: str):
        """Remove a player from this game session"""
        with self.lock:
            if player_id in self.player_streams:
                del self.player_streams[player_id]

                # If this was the Pacman player, reassign role if possible
                if player_id == self.pacman_player and self.current_players > 1:
                    # Find another player to be Pacman
                    for pid in self.player_roles:
                        if pid in self.player_streams:  # Make sure player is still connected
                            self.player_roles[pid] = pacman_pb2.PACMAN
                            self.pacman_player = pid
                            logger.info(f"Player {pid} is now PACMAN")
                            break
                    else:  # No players left with streams
                        self.pacman_player = None
                elif player_id == self.pacman_player:
                    self.pacman_player = None

                # Remove player role
                if player_id in self.player_roles:
                    del self.player_roles[player_id]

                # Remove player position and direction
                if player_id in self.player_positions:
                    del self.player_positions[player_id]
                if player_id in self.player_directions:
                    del self.player_directions[player_id]

                self.current_players -= 1

                # If no more players, set status to FINISHED
                if self.current_players == 0:
                    self.status = pacman_pb2.FINISHED
                # If only one player and no Pacman, the game can't continue properly
                elif self.pacman_player is None:
                    self.status = pacman_pb2.WAITING
                return True
            return False

    def update_loop(self):
        """Game state update loop that runs continuously in a separate thread"""
        try:
            # Update at 60 fps
            update_interval = 0.0167

            # Track the last time we processed each player's action
            last_action_time = {}

            while self.running:
                # Only process updates if game is active
                if self.status == pacman_pb2.IN_PROGRESS:
                    with self.lock:
                        # 1. Process player actions/movements
                        for player_id, role in list(self.player_roles.items()):
                            if player_id not in self.player_positions:
                                self.initialize_player_position(player_id)

                            # Process pending actions for this player
                            if player_id in self.player_actions:
                                action = self.player_actions[player_id]
                                self.move_player(player_id, action)
                                del self.player_actions[player_id]

                        # 2. Check for collisions between Pacman and ghosts
                        self.check_collisions()

                        # 3. Send updated game state to all players
                        self.broadcast_game_state()

                # Sleep to maintain update rate
                time.sleep(update_interval)
        except Exception as e:
            logger.error(f"Error in game update loop: {e}", exc_info=True)

    def move_player(self, player_id, direction):
        """Move a player based on their requested direction"""
        # Skip if player not found
        if player_id not in self.player_positions or player_id not in self.player_roles:
            return

        pos = self.player_positions[player_id]
        role = self.player_roles[player_id]

        # Convert gRPC direction enum to Pacman engine direction
        direction_map = {
            pacman_pb2.NORTH: Directions.NORTH,
            pacman_pb2.SOUTH: Directions.SOUTH,
            pacman_pb2.EAST: Directions.EAST,
            pacman_pb2.WEST: Directions.WEST,
            pacman_pb2.STOP: Directions.STOP
        }

        game_direction = direction_map.get(direction, Directions.STOP)

        # Get current position as a Configuration
        config = Configuration(pos, self.player_directions.get(player_id, Directions.STOP))

        # Check if the move is legal
        legal_actions = Actions.getPossibleActions(config, self.walls)

        if game_direction in legal_actions:
            # Set the player's new direction
            self.player_directions[player_id] = game_direction

            # Calculate the new position
            if game_direction != Directions.STOP:
                speed = 1.0  # Normal speed
                if role == pacman_pb2.GHOST and player_id in self.scared_timers and self.scared_timers[player_id] > 0:
                    speed = 0.5  # Scared ghosts move slower

                vector = Actions.directionToVector(game_direction, speed)
                new_pos = (pos[0] + vector[0], pos[1] + vector[1])

                # Update position
                self.player_positions[player_id] = new_pos

                # If this is Pacman, check for food consumption
                if role == pacman_pb2.PACMAN:
                    self.check_food_consumption(new_pos)

    def check_food_consumption(self, pos):
        """Check if Pacman is eating food or capsules"""
        # Convert to integer coordinates for grid access
        x, y = int(round(pos[0])), int(round(pos[1]))

        # Eat food pellet
        if self.food[x][y]:
            self.food[x][y] = False
            self.score += 10

            # Check win condition - all food eaten
            remaining_food = 0
            for row in self.food:
                for cell in row:
                    if cell:
                        remaining_food += 1

            if remaining_food == 0:
                self.score += 500  # Bonus for clearing the level
                self.status = pacman_pb2.FINISHED

        # Eat power capsule
        capsule_pos = (x, y)
        if capsule_pos in self.capsules:
            self.capsules.remove(capsule_pos)
            self.score += 50

            # Make all ghosts scared
            for player_id, role in self.player_roles.items():
                if role == pacman_pb2.GHOST:
                    self.scared_timers[player_id] = 40  # Scared for 40 frames (4 seconds at 10fps)

    def check_collisions(self):
        """Check for collisions between Pacman and ghosts"""
        if not self.pacman_player or self.pacman_player not in self.player_positions:
            return

        pacman_pos = self.player_positions[self.pacman_player]

        # Check collisions with each ghost
        for player_id, role in self.player_roles.items():
            if role == pacman_pb2.GHOST and player_id in self.player_positions:
                ghost_pos = self.player_positions[player_id]

                # Use manhattan distance for simplicity
                if manhattanDistance(pacman_pos, ghost_pos) < 1.0:  # Close enough for collision
                    if player_id in self.scared_timers and self.scared_timers[player_id] > 0:
                        # Pacman eats the ghost
                        self.score += 200
                        # Reset ghost position
                        self.initialize_player_position(player_id)
                        # Reset scared timer
                        self.scared_timers[player_id] = 0
                    else:
                        # Ghost eats Pacman
                        self.status = pacman_pb2.FINISHED  # Game over
                        break

    def process_player_action(self, player_id, action_type, direction):
        """Process a player action (movement or other command)"""
        if action_type == pacman_pb2.MOVE:
            # Get the player's role
            role = self.player_roles.get(player_id)

            # Only allow players to move characters matching their role
            if role == pacman_pb2.PACMAN and player_id == self.pacman_player:
                # This player is Pacman, allow them to move
                self.player_actions[player_id] = direction
                logger.info(f"Pacman player {player_id} moved: {direction}")
            elif role == pacman_pb2.GHOST:
                # This player is a ghost, allow them to move their ghost
                # Find which ghost this player controls
                ghost_players = [pid for pid, r in self.player_roles.items()
                                if r == pacman_pb2.GHOST and pid in self.player_positions]
                if player_id in ghost_players:
                    # Let them move their ghost
                    self.player_actions[player_id] = direction
                    logger.info(f"Ghost player {player_id} moved: {direction}")
            else:
                logger.warning(f"Player {player_id} tried to move but has invalid role: {role}")

    def broadcast_game_state(self):
        """Send current game state to all connected players"""
        with self.lock:
            # Update scared timers
            for player_id in list(self.scared_timers.keys()):
                if self.scared_timers[player_id] > 0:
                    self.scared_timers[player_id] -= 1

            # Create current game state
            game_state = self._create_game_state()

            # Broadcast to all players
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

    def _create_game_state(self):
        """Create the current game state for broadcasting to clients"""
        # Convert food grid to list of positions
        food_positions = []
        for x in range(self.food.width):
            for y in range(self.food.height):
                if self.food[x][y]:
                    food_positions.append(pacman_pb2.Position(x=x, y=y))

        # Convert capsules to Position objects
        capsule_positions = [pacman_pb2.Position(x=x, y=y) for x, y in self.capsules]

        # Convert walls to Position objects
        wall_positions = []
        for x in range(self.walls.width):
            for y in range(self.walls.height):
                if self.walls[x][y]:
                    wall_positions.append(pacman_pb2.Position(x=x, y=y))

        # Create agent objects for each player
        agents = []
        for player_id, role in self.player_roles.items():
            if player_id in self.player_positions and player_id in self.player_directions:
                pos = self.player_positions[player_id]
                direction = self.player_directions[player_id]

                # Convert Pacman direction to gRPC direction enum
                direction_map = {
                    Directions.NORTH: pacman_pb2.NORTH,
                    Directions.SOUTH: pacman_pb2.SOUTH,
                    Directions.EAST: pacman_pb2.EAST,
                    Directions.WEST: pacman_pb2.WEST,
                    Directions.STOP: pacman_pb2.STOP
                }

                scared_timer = 0
                is_scared = False
                if role == pacman_pb2.GHOST and player_id in self.scared_timers:
                    scared_timer = self.scared_timers[player_id]
                    is_scared = scared_timer > 0

                agent = pacman_pb2.AgentState(
                    player_id=player_id,
                    agent_id=player_id,
                    agent_type=role,
                    position=pacman_pb2.Position(x=pos[0], y=pos[1]),
                    direction=direction_map.get(direction, pacman_pb2.STOP),
                    scared_timer=scared_timer,
                    is_scared=is_scared
                )
                agents.append(agent)

        # Return the complete game state
        return pacman_pb2.GameState(
            game_id=self.game_id,
            status=self.status,
            agents=agents,
            food=food_positions,
            capsules=capsule_positions,
            walls=wall_positions,
            score=self.score,
            winner_id=self.pacman_player if self.status == pacman_pb2.FINISHED and self.score > 0 else ""
        )

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
            # Send initial game state to client so they see the board immediately
            initial_state = game._create_game_state()
            state_queue.put(initial_state)
            # Handle player actions in a separate thread
            def process_actions():
                try:
                    for action in request_iterator:
                        # Handle player's action based on action type
                        if action.action_type == pacman_pb2.LEAVE:
                            logger.info(f"Player {player_id} left game {game_id}")
                            game.remove_player(player_id)
                            break
                        elif action.action_type == pacman_pb2.MOVE:
                            # Process the player's move using the game logic
                            logger.info(f"Player {player_id} moved {action.direction} in game {game_id}")
                            game.process_player_action(player_id, action.action_type, action.direction)
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
                except Empty:
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
        server.stop(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pAIcMan Game Server")
    parser.add_argument("--port", type=int, default=50051, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default="./data", help="Directory to store data")

    args = parser.parse_args()
    serve(args.port, args.data_dir)