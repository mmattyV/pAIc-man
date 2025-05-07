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
import json
from typing import Dict, List, Optional, Tuple, Any
from queue import Queue, Empty
from pathlib import Path

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

# -------------  Raft additions  ----------------
from pysyncobj import SyncObj, SyncObjConf, replicated
# ----------------------------------------------

# Import Pacman game components
from helpers.game import Directions, Actions, AgentState, Configuration
from helpers.game import Grid
import helpers.layout as layout
from util import nearestPoint, manhattanDistance

# Import AI Pacman agents
from ai_pacman import create_ai_agent

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
    def __init__(self, game_id: str, layout_name: str, max_players: int, game_mode=pacman_pb2.PVP, ai_difficulty=pacman_pb2.MEDIUM, servicer=None):
        self.game_id = game_id
        self.layout_name = layout_name
        # Ensure max_players is between 2 and 4 (inclusive)
        self.max_players = max(2, min(4, max_players))
        self.current_players = 0
        self.lock = threading.RLock()
        self.player_streams = {}
        self.player_roles = {}  # Maps player_id to role (PACMAN or GHOST)
        self.player_directions = {}  # Maps player_id to current direction
        self.player_positions = {}  # Maps player_id to current position
        self.pacman_player = None  # Track which player is Pacman
        self.status = pacman_pb2.WAITING
        self.servicer = servicer   # Reference to the servicer for Raft leader check

        # Game mode and AI settings
        self.game_mode = game_mode
        self.ai_difficulty = ai_difficulty
        self.ai_agent = None

        # For AI Pacman mode, create the agent
        if self.game_mode == pacman_pb2.AI_PACMAN:
            difficulty_map = {
                pacman_pb2.EASY: "EASY",
                pacman_pb2.MEDIUM: "MEDIUM",
                pacman_pb2.HARD: "HARD"
            }
            self.ai_agent = create_ai_agent(difficulty_map.get(self.ai_difficulty, "MEDIUM"))
            # Create a virtual player ID for the AI Pacman
            self.pacman_player = "ai_pacman"
            logger.info(f"Created AI Pacman agent with difficulty {difficulty_map.get(self.ai_difficulty, 'MEDIUM')}")

            # For AI mode, we need at least 2 players (AI Pacman + 1 Ghost)
            # But we should still respect the user's max_players setting as an upper bound
            # Keep the range between 2 and max_players
            self.max_players = min(self.max_players, 3)

        # Game state variables
        self.score = 0

        # Initialize the game layout
        self.layout = layout.getLayout(layout_name)
        if not self.layout:
            logger.warning(f"Could not find layout {layout_name}, using mediumClassic as fallback")
            self.layout = layout.getLayout("mediumClassic")

        # Initialize AI Pacman position if in AI mode - MUST do this before creating any game state
        if self.game_mode == pacman_pb2.AI_PACMAN:
            self.initialize_ai_pacman_position()

        # Initialize game state including food grid
        self.initialize_game_state()

        # Player action queue
        self.player_actions = {}

        # State sync counter and version tracking to prevent rubber-banding
        self.state_sync_counter = 0
        self.state_version = int(time.time() * 1000)  # Use milliseconds as integer
        self.last_applied_version = 0
        self.next_sync_delay = 60  # Initial sync after 60 frames (1 second)

        # Start game update thread
        self.running = True
        self.update_thread = threading.Thread(target=self.update_loop)
        self.update_thread.daemon = True
        self.update_thread.start()
        logger.info(f"Created new game session {game_id} with mode {self.game_mode}")

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

    def get_direction_proto(self, game_dir: str) -> int:
        """Map internal Directions to protobuf enum"""
        mapping = {
            Directions.NORTH: pacman_pb2.NORTH,
            Directions.SOUTH: pacman_pb2.SOUTH,
            Directions.EAST:  pacman_pb2.EAST,
            Directions.WEST:  pacman_pb2.WEST,
            Directions.STOP:  pacman_pb2.STOP,
        }
        return mapping.get(game_dir, pacman_pb2.STOP)


    def add_player(self, player_id: str, stream, force_ghost=False):
        """Add a player to this game session"""
        with self.lock:
            if self.current_players >= self.max_players:
                return False

            # Role assignment depends on game mode
            if self.game_mode == pacman_pb2.PVP:
                # In PVP mode: first player is Pacman, rest are ghosts (unless forced)
                if self.pacman_player is None:
                    self.player_roles[player_id] = pacman_pb2.PACMAN
                    self.pacman_player = player_id
                    logger.info(f"Player {player_id} joined as PACMAN")
                else:
                    self.player_roles[player_id] = pacman_pb2.GHOST
                    logger.info(f"Player {player_id} joined as GHOST")
            elif self.game_mode == pacman_pb2.AI_PACMAN:
                # In AI Pacman mode: all human players are ghosts
                self.player_roles[player_id] = pacman_pb2.GHOST
                logger.info(f"Player {player_id} joined as GHOST in AI Pacman mode")
            else:
                # Unknown mode: default to ghost (force_ghost is implicit here)
                self.player_roles[player_id] = pacman_pb2.GHOST
                logger.info(f"Player {player_id} joined as GHOST (unknown mode)")

            # Register the stream and increment count
            self.player_streams[player_id] = stream
            self.current_players += 1

            # Initialize player position
            self.initialize_player_position(player_id)

            # Check if we can start the game
            if self.game_mode == pacman_pb2.PVP:
                # Need at least one Pacman and one ghost
                if self.pacman_player is not None and self.current_players > 1:
                    self.status = pacman_pb2.IN_PROGRESS
            elif self.game_mode == pacman_pb2.AI_PACMAN:
                # Can start with just one ghost
                if self.current_players >= 1:
                    self.status = pacman_pb2.IN_PROGRESS

            return True

    def initialize_player_position(self, player_id):
        """Initialize a player's position based on their role"""
        role = self.player_roles.get(player_id)
        if role is None:
            return

        # Skip position initialization for AI Pacman - it's handled separately
        if player_id == "ai_pacman":
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
        FPS = 10  # Frames per second
        update_interval = 1.0 / FPS
        ai_update_interval = 0.3  # 300ms for AI Pacman
        last_ai_update = time.time()

        while self.running:
            # Followers stay idle; they'll catch up via replicated log
            if self.servicer and not self.servicer._isLeader():
                time.sleep(0.05)
                continue

            try:
                game_start_time = time.time()

                # Only update game if it's in progress
                if self.status == pacman_pb2.IN_PROGRESS:
                    with self.lock:
                        # 1. Bump state version
                        self.state_version += 1

                        # 2. Process player actions
                        for pid, role in list(self.player_roles.items()):
                            if pid not in self.player_positions:
                                self.initialize_player_position(pid)
                            if pid in self.player_actions:
                                self.move_player(pid, self.player_actions.pop(pid))

                        # 3. Process AI Pacman at lower frequency
                        now = time.time()
                        if (
                            self.game_mode == pacman_pb2.AI_PACMAN
                            and self.ai_agent
                            and self.pacman_player == "ai_pacman"
                            and now - last_ai_update >= ai_update_interval
                        ):
                            self.update_ai_pacman()
                            last_ai_update = now

                        # 4. Check collisions
                        self.check_collisions()

                        # 5. Decrement scared timers
                        for pid in list(self.scared_timers):
                            if self.scared_timers[pid] > 0:
                                self.scared_timers[pid] -= 1

                        # 6. Adaptive state sync
                        self.state_sync_counter += 1
                        if self.servicer and self.state_sync_counter >= self.next_sync_delay:
                            state = self._serialize_game_state()
                            self.servicer._replicate_game_state(
                                self.game_id, state, self.state_version
                            )
                            logger.debug(f"Replicated state v{self.state_version} for game {self.game_id}")
                            self.state_sync_counter = 0
                            # at least every 2s: 120 ticks at 60Hz, here scaled by players
                            self.next_sync_delay = max(30, 120 // max(1, self.current_players))

                        # 7. Broadcast to clients
                        self.broadcast_game_state()

                # maintain target FPS
                elapsed = time.time() - game_start_time
                time.sleep(max(0, update_interval - elapsed))

            except Exception as e:
                logger.error(f"Error in game update loop: {e}", exc_info=True)
                time.sleep(0.1)

        logger.info(f"Update loop ended for game {self.game_id}")


    def move_player(self, player_id, direction):
        """Move a player based on their requested direction"""
        # Skip if player not found
        if player_id not in self.player_positions or player_id not in self.player_roles:
            return

        pos = self.player_positions[player_id]
        role = self.player_roles.get(player_id)

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
                speed = 1.0  # Normal speed in PVP mode

                vector = Actions.directionToVector(game_direction, speed)
                new_pos = (pos[0] + vector[0], pos[1] + vector[1])

                # Validate the new position is not in a wall
                x, y = int(round(new_pos[0])), int(round(new_pos[1]))
                if self.walls[x][y]:
                    logger.warning(f"Attempted move into wall at {x},{y} for player {player_id}! Ignoring.")
                    return

                # Update position
                self.player_positions[player_id] = new_pos
                logger.debug(f"Player {player_id} moved to {new_pos} with direction {game_direction}")

                # If this is Pacman, check for food consumption
                if role == pacman_pb2.PACMAN:
                    self.check_food_consumption(new_pos)

    def update_ai_pacman(self):
        """Update AI-controlled Pacman movement"""
        if not self.ai_agent:
            return

        # Create a simplified game state representation for the AI agent
        ai_game_state = self.create_ai_game_state()

        # Get the AI agent's action
        action = self.ai_agent.get_action(ai_game_state)

        # Update the AI Pacman's direction and position
        if action != Directions.STOP:
            # Get the current position of Pacman
            pacman_pos = self.get_pacman_position()
            if pacman_pos is None:
                # If Pacman position not yet initialized, initialize it
                self.initialize_ai_pacman_position()
                pacman_pos = self.get_pacman_position()

            if pacman_pos:
                # Update direction
                self.player_directions["ai_pacman"] = action

                # Calculate new position
                vector = Actions.directionToVector(action, 1.0)
                new_pos = (pacman_pos[0] + vector[0], pacman_pos[1] + vector[1])

                # Check if move is valid (not into a wall)
                x, y = int(round(new_pos[0])), int(round(new_pos[1]))
                if not self.walls[x][y]:
                    self.player_positions["ai_pacman"] = new_pos
                    # Check for food consumption at new position
                    self.check_food_consumption(new_pos)
                    # After any movement or state change, increment the state version to ensure proper ordering
                    self.state_version += 1

    def create_ai_game_state(self):
        """Create a simplified game state representation for the AI agent"""
        # Simple object with the necessary data for the AI agent
        class SimpleGameState:
            pass

        state = SimpleGameState()

        # Get Pacman's position and legal actions
        pacman_pos = self.get_pacman_position()
        if pacman_pos is None:
            # Initialize AI Pacman position if not set
            self.initialize_ai_pacman_position()
            pacman_pos = self.get_pacman_position()

        state.pacman_position = pacman_pos

        # Get ghost positions and scared timers
        state.ghost_positions = []
        state.ghost_scared_timers = []
        for player_id, role in self.player_roles.items():
            if role == pacman_pb2.GHOST and player_id in self.player_positions:
                ghost_pos = self.player_positions[player_id]
                state.ghost_positions.append(ghost_pos)
                # Add scared timer (defaulting to 0 for now - not scared)
                state.ghost_scared_timers.append(0)

        # Get food positions (convert from grid to list of positions)
        state.food_positions = []
        if self.food:
            for x in range(self.food.width):
                for y in range(self.food.height):
                    if self.food[x][y]:
                        state.food_positions.append((x, y))

        # Get capsule positions
        state.capsule_positions = self.capsules[:] if self.capsules else []

        # Get legal actions for Pacman
        state.pacman_legal_actions = []
        if pacman_pos:
            x, y = int(pacman_pos[0]), int(pacman_pos[1])
            for action in [Directions.NORTH, Directions.SOUTH, Directions.EAST, Directions.WEST]:
                dx, dy = Actions.directionToVector(action)
                next_x, next_y = int(x + dx), int(y + dy)
                if not self.walls[next_x][next_y]:
                    state.pacman_legal_actions.append(action)

        return state

    def check_food_consumption(self, pos):
        """Check if Pacman is eating food or capsules"""
        # Convert to integer coordinates for grid access
        x, y = int(round(pos[0])), int(round(pos[1]))

        # Eat food pellet
        if self.food[x][y]:
            self.food[x][y] = False
            self.score += 10
            # Increment state version whenever score/state changes
            self.state_version += 1

            # Check if all food eaten
            remaining_food = 0
            for row in self.food:
                for cell in row:
                    if cell:
                        remaining_food += 1

            if remaining_food == 0:
                # Instead of ending the game, reset the food grid
                logger.info(f"All food eaten in game {self.game_id}. Resetting food grid.")
                self.score += 200  # Bonus for clearing the level
                self.state_version += 1

                # Reset food to initial layout
                self.food = self.layout.food.copy()

                # Optionally, reset capsules
                self.capsules = self.layout.capsules[:]

                # Broadcast the updated state to all players
                self.broadcast_game_state()

        # Eat power capsule
        capsule_pos = (x, y)
        if capsule_pos in self.capsules:
            self.capsules.remove(capsule_pos)
            self.score += 50
            self.state_version += 1

            # Make all ghosts scared
            for player_id, role in self.player_roles.items():
                if role == pacman_pb2.GHOST and player_id != "ai_pacman":  # Ensure we don't make AI scared
                    self.scared_timers[player_id] = 200  # Scared for 20 seconds at 10fps

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
                        # Increment state version
                        self.state_version += 1
                    else:
                        # Ghost eats Pacman
                        self.status = pacman_pb2.FINISHED  # Game over
                        # Increment state version
                        self.state_version += 1
                        break

    def process_player_action(self, player_id, action_type, direction):
        """Process a player action (movement or other command)"""
        if action_type == pacman_pb2.MOVE:
            # Get the player's role
            role = self.player_roles.get(player_id)

            # Only allow players to move characters matching their role
            if role == pacman_pb2.PACMAN and player_id == self.pacman_player and self.game_mode == pacman_pb2.PVP:
                # This player is Pacman in PVP mode, allow them to move
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
        with self.lock:
            # Create agent states for all agents

            agents = []

            # Add Pacman agent (AI or player)
            if self.pacman_player and self.pacman_player in self.player_positions:
                pacman_pos = self.player_positions[self.pacman_player]
                pacman_direction = self.player_directions.get(self.pacman_player, Directions.STOP)
                # Determine if the current Pacman is AI controlled
                is_ai_pacman = (self.game_mode == pacman_pb2.AI_PACMAN and self.pacman_player == "ai_pacman")

                pacman_agent = pacman_pb2.AgentState(
                    agent_id=self.pacman_player, # This is 'ai_pacman' or actual player_id
                    agent_type=pacman_pb2.PACMAN,
                    position=pacman_pb2.Position(x=pacman_pos[0], y=pacman_pos[1]),
                    direction=self.get_direction_proto(pacman_direction),
                    is_scared=False, # Pacman itself is not scared this way
                    scared_timer=0,
                    player_id= "AI" if is_ai_pacman else self.pacman_player # For display/identification
                )
                agents.append(pacman_agent)
            elif self.game_mode == pacman_pb2.AI_PACMAN and "ai_pacman" not in self.player_positions:
                # This case might occur if AI Pacman was just eaten or removed
                logger.warning("AI Pacman (ai_pacman) not found in player_positions during _create_game_state for AI_PACMAN mode.")

            # Add ghost agents
            for player_id, role in self.player_roles.items():
                if role == pacman_pb2.GHOST and player_id in self.player_positions:
                    ghost_pos = self.player_positions[player_id]
                    ghost_direction = self.player_directions.get(player_id, Directions.STOP)

                    # Safely get scared_timer and derive is_scared
                    scared_timer = self.scared_timers.get(player_id, 0)
                    is_scared = scared_timer > 0

                    # Add agent data for a player-controlled ghost
                    ghost_agent = pacman_pb2.AgentState(
                        agent_id=f"ghost_{player_id}",
                        agent_type=pacman_pb2.GHOST,
                        position=pacman_pb2.Position(x=ghost_pos[0], y=ghost_pos[1]),
                        direction=self.get_direction_proto(ghost_direction),
                        is_scared=is_scared,
                        scared_timer=scared_timer,
                        player_id=player_id
                    )
                    agents.append(ghost_agent)

            # Create food positions list
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

            # Create the game state message
            return pacman_pb2.GameState(
                game_id=self.game_id,
                agents=agents,
                food=food_positions,
                capsules=capsule_positions,
                walls=wall_positions,
                score=self.score,
                status=self.status,
                winner_id=self.get_winner_id(),
                game_mode=self.game_mode,
                ai_difficulty=self.ai_difficulty
            )

    def get_info(self):
        """Return information about this game session"""
        with self.lock:
            return pacman_pb2.GameInfo(
                game_id=self.game_id,
                layout_name=self.layout_name,
                current_players=self.current_players,
                max_players=self.max_players,
                status=self.status
            )

    def _serialize_game_state(self) -> str:
        """Serialize essential game state to JSON string"""
        with self.lock:
            # Convert food grid to 2D array
            food_array = []
            for y in range(self.food.height):
                row = []
                for x in range(self.food.width):
                    row.append(bool(self.food[x][y]))
                food_array.append(row)

            # Convert positions to serializable format
            serialized_positions = {}
            for pid, pos in self.player_positions.items():
                serialized_positions[pid] = [float(pos[0]), float(pos[1])]

            # Serialize capsules
            serialized_capsules = [[float(x), float(y)] for x, y in self.capsules]

            # Serialize player directions (convert Directions enum to string)
            serialized_directions = {}
            for pid, direction in self.player_directions.items():
                serialized_directions[pid] = direction

            # Create serializable state dictionary
            state_dict = {
                'food_array': food_array,
                'score': self.score,
                'capsules': serialized_capsules,
                'player_positions': serialized_positions,
                'player_directions': serialized_directions,
                'scared_timers': self.scared_timers.copy() if hasattr(self, 'scared_timers') else {},
                'state_version': self.state_version
            }

            return json.dumps(state_dict)

    def _apply_serialized_game_state(self, serialized_state: str, state_version: int) -> None:
        """Apply a serialized game state to this game session if it's newer than current state"""
        with self.lock:
            try:
                # Skip if we've already applied a newer state version
                if state_version <= self.last_applied_version:
                    logger.debug(f"Skipping older state version {state_version} (current: {self.last_applied_version})")
                    return

                # Check if this is a big version jump - log for debugging
                if self.last_applied_version > 0 and state_version - self.last_applied_version > 100:
                    logger.warning(f"Large state version jump: {self.last_applied_version} -> {state_version}")

                state_dict = json.loads(serialized_state)

                # Update food grid
                food_array = state_dict['food_array']
                for y in range(min(len(food_array), self.food.height)):
                    row = food_array[y]
                    for x in range(min(len(row), self.food.width)):
                        self.food[x][y] = row[x]

                # Update score
                self.score = state_dict['score']

                # Update capsules
                self.capsules = [(float(x), float(y)) for x, y in state_dict['capsules']]

                # Update player positions
                player_positions_changed = False
                for pid, pos in state_dict['player_positions'].items():
                    if pid in self.player_positions:
                        current_pos = self.player_positions[pid]
                        new_pos = (float(pos[0]), float(pos[1]))
                        if current_pos != new_pos:
                            player_positions_changed = True
                    self.player_positions[pid] = (float(pos[0]), float(pos[1]))

                # Update player directions
                for pid, direction in state_dict['player_directions'].items():
                    self.player_directions[pid] = direction

                # Update scared timers
                if 'scared_timers' in state_dict:
                    # Initialize scared_timers if it doesn't exist
                    if not hasattr(self, 'scared_timers'):
                        self.scared_timers = {}
                    for pid, timer in state_dict['scared_timers'].items():
                        self.scared_timers[pid] = timer

                # Update local state version
                old_version = self.last_applied_version
                self.last_applied_version = state_version

                # If we're on a follower node, also update self.state_version
                if self.servicer and not self.servicer._isLeader():
                    self.state_version = state_version

                # Log position changes for debugging
                if player_positions_changed:
                    logger.debug(f"Applied state v{old_version}->{state_version} to game {self.game_id} (positions updated)")
                else:
                    logger.debug(f"Applied state v{old_version}->{state_version} to game {self.game_id} (no position changes)")

            except Exception as e:
                logger.error(f"Error applying serialized state: {e}", exc_info=True)

    def get_winner_id(self):
        """Get the ID of the winner if the game is finished"""
        if self.status != pacman_pb2.FINISHED:
            return ""

        # In AI mode, if Pacman (AI) is removed (e.g., caught by a ghost it controls),
        # or if Pacman is human and removed.
        if self.pacman_player not in self.player_positions:
            # If Pacman is gone, a ghost player wins. Find any ghost.
            ghost_players = [pid for pid, role in self.player_roles.items() if role == pacman_pb2.GHOST and pid in self.player_positions]
            return ghost_players[0] if ghost_players else "" # Or some indicator of no clear winner if no ghosts remain

        # If Pacman is still in play and game is FINISHED, Pacman wins.
        # (This could happen if all ghosts are eaten, or by other win conditions not yet defined)
        return self.pacman_player

    def get_pacman_position(self):
        """Get the current position of Pacman"""
        # self.pacman_player stores "ai_pacman" in AI mode or the player_id in PVP
        if self.pacman_player:
            return self.player_positions.get(self.pacman_player)
        return None

    def initialize_ai_pacman_position(self):
        """Initialize the position of AI-controlled Pacman"""
        # Start AI Pacman at the layout's Pacman start position
        for is_pacman, pos in self.layout.agentPositions:
            if is_pacman:
                self.player_positions["ai_pacman"] = pos
                self.player_directions["ai_pacman"] = Directions.STOP
                break

class PacmanServicer(pacman_pb2_grpc.PacmanGameServicer, SyncObj):
    """Implementation of the PacmanGame service"""
    def __init__(self, port: int, data_dir: str, self_addr: str, partner_addrs: List[str]):
        # --- initialise Raft first ---
        conf = SyncObjConf(
            dataDir=data_dir,
            autoTick=True,  # Auto-tick for raft operations
            tickInterval=0.1, # Check for raft operations every 100ms
            dynamicMembershipChange = True # Allow dynamic changes to partners
        )
        # The SyncObj constructor (__init__ in the super call) stores the self_addr
        # in an internal attribute, accessible via self.selfNode
        super(PacmanServicer, self).__init__(self_addr, partner_addrs, conf)
        # ------------------------------

        self.games: Dict[str, GameSession] = {}
        self.games_lock = threading.RLock() # Lock for accessing the games dictionary
        self.port = port
        # Access the self address via self.selfNode as provided by SyncObj
        logger.info(f"PacmanServicer initialized on {self.selfNode} with partners {partner_addrs}")


    @replicated
    def _create_game(self, game_id: str, layout_name: str, max_players: int, game_mode: pacman_pb2.GameMode, ai_difficulty: pacman_pb2.AIDifficulty) -> None:
        """Replicated helper that actually inserts the GameSession."""
        # Access the self address via self.selfNode as provided by SyncObj
        node_addr = str(self.selfNode) # Correctly obtain node address here
        with self.games_lock:
            if game_id not in self.games:
                # Note: GameSession creation happens on *all* nodes applying the log
                # It must be deterministic based on the replicated args.
                self.games[game_id] = GameSession(game_id, layout_name, max_players, game_mode, ai_difficulty, servicer=self)
                logger.info("Raft: Replicated game creation for %s on node %s", game_id, node_addr) # Use the correctly obtained node_addr
            else:
                # This was the line with the AttributeError
                # Use the correctly obtained node_addr instead of the non-existent _selfNodeAddr
                logger.warning("Raft: Game %s already exists on node %s, not creating.", game_id, node_addr) # Corrected line

    def CreateGame(self, request: pacman_pb2.GameConfig, context):
        """RPC method to create a new game"""
        try:
            game_id = str(uuid.uuid4())
            layout_name = request.layout_name
            max_players = request.max_players

            # Get game mode and AI difficulty from request (defaults to PVP/MEDIUM if not provided)
            # Using getattr for backwards compatibility if new fields are added to proto
            game_mode = getattr(request, 'game_mode', pacman_pb2.PVP)
            ai_difficulty = getattr(request, 'ai_difficulty', pacman_pb2.MEDIUM)

            logger.info(f"Create game request: layout={layout_name}, max_players={max_players}, mode={game_mode}, difficulty={ai_difficulty}")

            # Validate request
            if max_players <= 0 or max_players > 4:
                logger.warning(f"Invalid max_players value received: {max_players}")
                return pacman_pb2.GameSession(
                    game_id="",
                    success=False,
                    error_message="Invalid max_players value. Must be between 1 and 4."
                )

            # For AI Pacman mode, max players should be different (only ghosts allowed for players)
            # Adjust max_players: if it's set > 0, cap it to 3 (for human ghosts)
            # The AI Pacman slot is separate.
            if game_mode == pacman_pb2.AI_PACMAN:
                # This logic might need refinement depending on how AI_PACMAN works.
                # Assuming max_players here means max *human* players (ghosts).
                # A value of 0 could mean just AI vs AI, or AI Pacman vs N human ghosts.
                # Let's assume it means max human ghosts. Cap at 3.
                 if max_players > 3:
                     logger.info(f"AI_PACMAN mode: Adjusting max_players from {max_players} to 3 (max human ghosts).")
                     max_players = 3
                 elif max_players == 0:
                     # If 0 human players are requested in AI_PACMAN mode,
                     # you might still need slots for AI, or maybe the game
                     # logic handles AI participants separately. Let's allow 0
                     # max_players for humans if requested, assuming the AI is handled
                     # outside the 'max_players' count.
                     pass # Allow 0 if specifically requested
                 # Minimum 1 human player is not required for AI_PACMAN if 0 is requested.


            # Create new game session via replicated call
            # This call is processed by the Raft leader and then replicated
            self._create_game(game_id, layout_name, max_players, game_mode, ai_difficulty)

            # Note: The RPC returns *immediately* after the replicated call is initiated.
            # It does NOT wait for consensus. The actual GameSession is created
            # asynchronously later on all nodes once the log entry is committed.
            # If the client needs to know the game is *ready*, a different mechanism
            # (like a status field or another RPC to check status) is needed.

            logger.info(f"Initiated creation of game: {game_id}, layout: {layout_name}, max players: {max_players}, mode: {game_mode}, AI difficulty: {ai_difficulty}")

            return pacman_pb2.GameSession(
                game_id=game_id,
                success=True,
                error_message="" # Success, so no error message
            )
        except Exception as e:
            logger.error(f"Error creating game:", exc_info=True) # Use exc_info=True to log traceback
            # Access the self address via self.selfNode for logging the error location
            node_addr = str(self.selfNode)
            return pacman_pb2.GameSession(
                game_id="",
                success=False,
                error_message=f"Server error on node {node_addr}: {str(e)}" # Include node info in error message
            )

    def ListGames(self, request, context):
        """RPC method to list available games"""
        try:
            games_list = []
            # Reading state does NOT need to be replicated. Any node can serve reads.
            with self.games_lock:
                for game_id, game in self.games.items():
                    # Only include games that aren't full or finished
                    # Assuming GameSession.get_info() is thread-safe or returns a safe snapshot
                    if game.status != pacman_pb2.FINISHED: # Assuming FINISHED is a class attribute or similar
                         # You might also want to check if the game is full here if you only
                         # want to show games that players can *join*. The original code doesn't,
                         # so keeping it as is, but it's a common requirement.
                         # if len(game.player_roles) < game.max_players:
                         games_list.append(game.get_info())

            return pacman_pb2.GamesList(games=games_list)
        except Exception as e:
            logger.error(f"Error listing games:", exc_info=True)
            # Access the self address via self.selfNode for logging the error location
            node_addr = str(self.selfNode)
            # Return an empty list and an error message in the response if your proto supports it,
            # or just an empty list as per the original code. Returning an error message
            # might be better for client debugging. Assuming the proto doesn't have error fields for ListGames.
            return pacman_pb2.GamesList(games=[]) # Return empty list on error as per original code

    # --- Replicated methods for player actions ---
    @replicated
    def _replicated_join(self, game_id: str, player_id: str, is_ghost: bool) -> None:
        """Replicates the join action to all nodes."""
        # Access the self address via self.selfNode as provided by SyncObj
        node_addr = str(self.selfNode) # Correctly obtain node address here
        with self.games_lock:
            if game_id in self.games:
                game = self.games[game_id]
                # The actual add_player logic (especially stream handling) is leader-specific
                # Followers just need to update their game state representation.
                # The add_player method *must* be deterministic.
                if player_id not in game.player_roles:
                    # stream=None for followers, the leader's add_player handles the real stream
                    game.add_player(player_id, stream=None, force_ghost=is_ghost)
                    logger.info("Raft: Replicated join for player %s in game %s (is_ghost: %s) on node %s",
                                player_id, game_id, is_ghost, node_addr) # Use the correctly obtained node_addr
                else:
                    logger.info("Raft: Player %s already in game %s on node %s, join replication skipped.",
                                player_id, game_id, node_addr) # Use the correctly obtained node_addr
            else:
                logger.warning("Raft: Game %s not found on node %s for replicating join.",
                                game_id, node_addr) # Use the correctly obtained node_addr

    @replicated
    def _replicated_leave(self, game_id: str, player_id: str) -> None:
        """Replicates the leave action to all nodes."""
        # Access the self address via self.selfNode as provided by SyncObj
        node_addr = str(self.selfNode) # Correctly obtain node address here
        with self.games_lock:
            if game_id in self.games:
                game = self.games[game_id]
                # The remove_player method *must* be deterministic.
                if player_id in game.player_roles:
                    game.remove_player(player_id)
                    logger.info("Raft: Replicated leave for player %s in game %s on node %s",
                                player_id, game_id, node_addr) # Use the correctly obtained node_addr
                    # You might need logic here to check if the game is now empty
                    # and should be cleaned up (also needs replication).
                else:
                    logger.info("Raft: Player %s not in game %s on node %s, leave replication skipped.",
                                player_id, game_id, node_addr) # Use the correctly obtained node_addr
            else:
                logger.warning("Raft: Game %s not found on node %s for replicating leave.",
                                game_id, node_addr) # Use the correctly obtained node_addr

    @replicated
    def _replicated_player_action(self, game_id: str, player_id: str, action_type: pacman_pb2.ActionType, direction: pacman_pb2.Direction) -> None:
        """Replicates a player action (like MOVE) to all nodes."""
        # Access the self address via self.selfNode as provided by SyncObj
        node_addr = str(self.selfNode) # Correctly obtain node address here
        with self.games_lock:
            if game_id in self.games:
                game = self.games[game_id]
                # The process_player_action method *must* be deterministic.
                if player_id in game.player_roles:
                    # The GameSession's process_player_action handles the actual logic
                    # This must be deterministic and update the game state identically
                    # on all replicas.
                    game.process_player_action(player_id, action_type, direction)
                    logger.info("Raft: Replicated action %s for player %s in game %s on node %s",
                                action_type, player_id, game_id, node_addr) # Use the correctly obtained node_addr
                    # Note: If player actions trigger immediate game state changes
                    # (like eating a pellet or collision), this logic must be inside
                    # process_player_action or another deterministic function
                    # called from here.
                else:
                     logger.warning("Raft: Player %s not found in game %s on node %s for replicating action.",
                                    player_id, game_id, node_addr) # Use the correctly obtained node_addr
            else:
                logger.warning("Raft: Game %s not found on node %s for replicating action.",
                                game_id, node_addr) # Use the correctly obtained node_addr

    @replicated
    def _replicate_game_state(self, game_id: str, serialized_state: str, state_version: int) -> None:
        """Replicate game state from leader to followers"""
        with self.games_lock:
            if game_id in self.games:
                game = self.games[game_id]
                game._apply_serialized_game_state(serialized_state, state_version)

    # --- End Replicated methods ---

    def PlayGame(self, request_iterator, context):
        """RPC method for bidirectional streaming gameplay"""
        player_id = None
        game_id = None
        game = None
        is_reconnection = False

        # Queue for game state updates to this player
        state_queue = Queue()

        try:
            # Process the first message which should be a JOIN action
            first_action = next(request_iterator)

            if first_action.action_type != pacman_pb2.JOIN and first_action.action_type != pacman_pb2.MOVE:
                yield pacman_pb2.GameState(
                    game_id="",
                    status=pacman_pb2.FINISHED,
                    winner_id=""
                )
                return

            player_id = first_action.player_id
            game_id = first_action.game_id

            # Check if we're the leader
            if not self._isLeader():
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Please connect to the leader node")
                return

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

                # Check if this is a reconnection - player already exists in game
                if player_id in game.player_roles:
                    logger.info(f"Player {player_id} is reconnecting to game {game_id}")
                    is_reconnection = True

                    # Update the stream for the existing player
                    if player_id in game.player_streams:
                        game.player_streams[player_id] = state_queue
                    else:
                        # Determine if this should be a ghost (any player after the first)
                        is_ghost = game.pacman_player is not None and game.pacman_player != player_id

                        # Add player back with their stream
                        game.add_player(player_id, state_queue, force_ghost=is_ghost)

                        # Also replicate the join action to followers
                        self._replicated_join(game_id, player_id, is_ghost=is_ghost)
                else:
                    # New player joining
                    # Determine if this should be a ghost (any player after the first)
                    is_ghost = game.pacman_player is not None

                    # Add player to game - the leader directly adds the player's stream
                    if not game.add_player(player_id, state_queue, force_ghost=is_ghost):
                        yield pacman_pb2.GameState(
                            game_id=game_id,
                            status=pacman_pb2.FINISHED,
                            winner_id=""
                        )
                        return

                    # Also replicate the join action to followers
                    self._replicated_join(game_id, player_id, is_ghost=is_ghost)

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
                            # Replicate leave action to followers
                            self._replicated_leave(game_id, player_id)
                            break
                        elif action.action_type == pacman_pb2.MOVE:
                            # Process the player's move using the game logic
                            logger.info(f"Player {player_id} moved {action.direction} in game {game_id}")
                            # Replicated so every node applies the same move
                            self._replicated_player_action(
                                game_id, player_id, action.action_type, action.direction)
                except grpc.RpcError as e:
                    # This is expected when clients disconnect, just log at debug level
                    logger.debug(f"Client {player_id} disconnected: gRPC error")
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

def serve(port: int, data_dir: str, self_addr: str, partner_addrs: List[str]):
    """Start the gRPC server"""
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs("logs", exist_ok=True)

    # Create server
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=10))

    # Add our service to the server
    servicer = PacmanServicer(port, data_dir, self_addr, partner_addrs)
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

def load_config():
    """Load configuration from config.json file"""
    config = {}
    config_path = Path(__file__).parent / "config.json"

    try:
        if config_path.exists():
            with open(config_path, "r") as f:
                config = json.load(f)
                logger.info(f"Loaded configuration from {config_path}")
    except Exception as e:
        logger.error(f"Error loading config: {e}")

    return config

if __name__ == "__main__":
    # Load configuration
    config = load_config()
    server_config = config.get("server", {})

    # Set defaults from config or use hardcoded values if not found
    default_port = server_config.get("port", 50051)
    default_data_dir = server_config.get("data_dir", "./data")
    default_self_addr = server_config.get("self_addr", "localhost:50051")
    default_partner_addrs = server_config.get("partner_addrs", [
        "localhost:50052",
        "localhost:50053",
        "localhost:50054",
        "localhost:50055"
    ])

    parser = argparse.ArgumentParser(description="pAIcMan Game Server")
    parser.add_argument("--port", type=int, default=default_port, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default=default_data_dir, help="Directory to store data")
    parser.add_argument("--self-addr", type=str, default=default_self_addr, help="Self address")
    parser.add_argument("--partner-addrs", type=str, nargs="+", default=default_partner_addrs, help="Partner addresses")

    args = parser.parse_args()
    serve(args.port, args.data_dir, args.self_addr, args.partner_addrs)