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
from typing import Dict, List, Optional, Tuple
from queue import Queue, Empty

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

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
    def __init__(self, game_id: str, layout_name: str, max_players: int, game_mode=pacman_pb2.PVP, ai_difficulty=pacman_pb2.MEDIUM):
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
            
            # Initialize the max players to 3 (1 AI Pacman + 2 Ghosts) for AI mode
            self.max_players = 3

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

    def add_player(self, player_id: str, stream):
        """Add a player to this game session"""
        with self.lock:
            if self.current_players >= self.max_players:
                return False

            # Role assignment depends on game mode
            if self.game_mode == pacman_pb2.PVP:
                # In PVP mode: first player is Pacman, rest are ghosts
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
                # Default to ghost if unknown mode
                self.player_roles[player_id] = pacman_pb2.GHOST
                logger.info(f"Player {player_id} joined as GHOST (unknown game mode)")

            self.player_streams[player_id] = stream
            self.current_players += 1

            # Initialize player position
            self.initialize_player_position(player_id)

            # Game can start with appropriate conditions
            if self.game_mode == pacman_pb2.PVP:
                # In PVP mode, need at least one Pacman and one ghost
                if self.pacman_player is not None and self.current_players > 1:
                    self.status = pacman_pb2.IN_PROGRESS
            elif self.game_mode == pacman_pb2.AI_PACMAN:
                # In AI Pacman mode, game can start with just one ghost player
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
        FPS = 10  # Frames per second - how frequently the game state updates
        update_interval = 1.0 / FPS  # Time between updates in seconds
        
        # For AI Pacman mode, we'll limit the update frequency
        ai_update_interval = 0.3  # Update AI Pacman less frequently (every 300ms) to make game more fair
        last_ai_update = time.time()

        while self.running:
            try:
                game_start_time = time.time()

                # Only update game if it's in progress
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
                                
                        # 2. Process AI Pacman movement only at certain intervals
                        # This makes the game more balanced
                        current_time = time.time()
                        if (self.game_mode == pacman_pb2.AI_PACMAN and 
                            self.ai_agent and 
                            self.pacman_player == "ai_pacman" and
                            current_time - last_ai_update >= ai_update_interval):
                            
                            self.update_ai_pacman()
                            last_ai_update = current_time
                            
                        # 3. Check for collisions between Pacman and ghosts
                        self.check_collisions()
                        
                        # 4. Update scared timers
                        for player_id in list(self.scared_timers.keys()):
                            if self.scared_timers[player_id] > 0:
                                self.scared_timers[player_id] -= 1
                                
                        # 5. Send updated game state to all clients
                        self.broadcast_game_state()

                # Calculate time to sleep to maintain FPS
                elapsed = time.time() - game_start_time
                sleep_time = max(0, update_interval - elapsed)
                time.sleep(sleep_time)

            except Exception as e:
                logger.error(f"Error in game update loop: {e}", exc_info=True)
                # Brief sleep to avoid tight loop in case of recurring errors
                time.sleep(0.1)

        logger.info(f"Update loop ended for game {self.game_id}")

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
                # Set speed based on role and game mode
                if self.game_mode == pacman_pb2.AI_PACMAN and role == pacman_pb2.GHOST:
                    speed = 2.0  # Faster ghosts in AI mode to keep up with AI Pacman
                    if player_id in self.scared_timers and self.scared_timers[player_id] > 0:
                        speed = 1.0  # Scared ghosts in AI mode move at normal speed
                else:
                    speed = 1.0  # Normal speed in PVP mode
                    if role == pacman_pb2.GHOST and player_id in self.scared_timers and self.scared_timers[player_id] > 0:
                        speed = 0.5  # Scared ghosts in PVP mode move slower

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
    
    def initialize_ai_pacman_position(self):
        """Initialize the AI-controlled Pacman's position"""
        # Find Pacman start position from layout
        pacman_start = self.layout.agentPositions[0][1]
        self.player_positions["ai_pacman"] = pacman_start
        self.player_directions["ai_pacman"] = Directions.STOP
        logger.info(f"Initialized AI Pacman position to {pacman_start}")
    
    def get_direction_proto(self, direction):
        """Convert internal direction to protocol buffer direction"""
        direction_map = {
            Directions.NORTH: pacman_pb2.NORTH,
            Directions.SOUTH: pacman_pb2.SOUTH,
            Directions.EAST: pacman_pb2.EAST,
            Directions.WEST: pacman_pb2.WEST,
            Directions.STOP: pacman_pb2.STOP
        }
        return direction_map.get(direction, pacman_pb2.STOP)
        
    def get_winner_id(self):
        """Get the ID of the winner if the game is finished"""
        if self.status != pacman_pb2.FINISHED:
            return ""
        
        # In AI mode, if game ended and AI is still in the game, ghosts win
        if self.game_mode == pacman_pb2.AI_PACMAN and "ai_pacman" not in self.player_positions:
            # Find a ghost player to declare winner
            ghost_players = [pid for pid, role in self.player_roles.items() if role == pacman_pb2.GHOST]
            return ghost_players[0] if ghost_players else ""
            
        # In PVP mode or if AI won, return pacman_player
        return self.pacman_player if self.pacman_player else ""
    
    def get_pacman_position(self):
        """Get the current position of Pacman"""
        if self.game_mode == pacman_pb2.AI_PACMAN:
            return self.player_positions.get("ai_pacman")
        else:
            return self.player_positions.get(self.pacman_player) if self.pacman_player else None
            
    def _create_game_state(self):
        """Create the current game state for broadcasting to clients"""
        with self.lock:
            # Create agent states for all agents
            agents = []

            # Add Pacman agent - either AI Pacman or player-controlled Pacman, not both
            if self.game_mode == pacman_pb2.AI_PACMAN:
                # For AI mode, only use the AI Pacman
                ai_pos = self.get_pacman_position()
                
                # Ensure we have a valid position for the AI pacman
                if ai_pos is None:
                    # Reinitialize AI Pacman position if not found
                    self.initialize_ai_pacman_position()
                    ai_pos = self.get_pacman_position()
                    logger.info(f"Reinitialized AI Pacman position to {ai_pos}")
                
                if ai_pos is not None:  # Only create agent if we have a position
                    ai_direction = self.player_directions.get("ai_pacman", Directions.STOP)
                    pacman_agent = pacman_pb2.AgentState(
                        agent_id="ai_pacman",
                        agent_type=pacman_pb2.PACMAN,
                        position=pacman_pb2.Position(x=ai_pos[0], y=ai_pos[1]),
                        direction=self.get_direction_proto(ai_direction),
                        is_scared=False,
                        scared_timer=0,
                        player_id="AI"  # Use "AI" for display purposes
                    )
                    agents.append(pacman_agent)
                
            # For PVP mode, add human-controlled Pacman if it exists
            elif self.pacman_player and self.pacman_player in self.player_positions:
                pacman_pos = self.player_positions[self.pacman_player]
                if pacman_pos is not None:
                    pacman_direction = self.player_directions.get(self.pacman_player, Directions.STOP)
                    pacman_agent = pacman_pb2.AgentState(
                        agent_id=self.pacman_player,
                        agent_type=pacman_pb2.PACMAN,
                        position=pacman_pb2.Position(x=pacman_pos[0], y=pacman_pos[1]),
                        direction=self.get_direction_proto(pacman_direction),
                        is_scared=False,
                        scared_timer=0,
                        player_id=self.pacman_player
                    )
                    agents.append(pacman_agent)

            # Add ghost agents
            for player_id, role in self.player_roles.items():
                if role == pacman_pb2.GHOST and player_id in self.player_positions:
                    ghost_pos = self.player_positions[player_id]
                    ghost_direction = self.player_directions.get(player_id, Directions.STOP)
                    is_scared = False
                    if player_id in self.scared_timers:
                        scared_timer = self.scared_timers[player_id]
                        is_scared = scared_timer > 0

                    # Add agent data for a player-controlled ghost
                    ghost_agent = pacman_pb2.AgentState(
                        agent_id=f"ghost_{player_id}",
                        agent_type=pacman_pb2.GHOST,
                        position=pacman_pb2.Position(x=ghost_pos[0], y=ghost_pos[1]),
                        direction=self.get_direction_proto(ghost_direction),
                        is_scared=is_scared,
                        scared_timer=scared_timer if is_scared else 0,
                        player_id=player_id
                    )
                    agents.append(ghost_agent)
            
            # The AI Pacman is already handled in the earlier section for AI mode, no need to add it again here

        # Create food positions list
        food_positions = []
        for x in range(self.food.width):
            for y in range(self.food.height):
                if self.food[x][y]:
                    food_positions.append(pacman_pb2.Position(x=x, y=y))

        # Convert capsules to Position objects
        capsule_positions = [pacman_pb2.Position(x=x, y=y) for x, y in self.capsules]

        # Create wall positions list
        walls = []
        for x in range(self.walls.width):
            for y in range(self.walls.height):
                if self.walls[x][y]:
                    walls.append(pacman_pb2.Position(x=x, y=y))

        # Create the game state message
        return pacman_pb2.GameState(
            game_id=self.game_id,
            agents=agents,
            food=food_positions,
            capsules=capsule_positions,
            walls=walls,
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
                status=self.status,
                game_mode=self.game_mode,
                ai_difficulty=self.ai_difficulty
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
            
            # Get game mode and AI difficulty from request (defaults to PVP/MEDIUM if not provided)
            game_mode = getattr(request, 'game_mode', pacman_pb2.PVP)
            ai_difficulty = getattr(request, 'ai_difficulty', pacman_pb2.MEDIUM)
            
            logger.info(f"Create game request with mode {game_mode}, difficulty {ai_difficulty}")

            # Validate request
            if max_players <= 0 or max_players > 4:
                return pacman_pb2.GameSession(
                    game_id="",
                    success=False,
                    error_message="Invalid max_players value. Must be between 1 and 4."
                )
                
            # For AI Pacman mode, max players should be different (only ghosts)
            if game_mode == pacman_pb2.AI_PACMAN:
                # Adjust max_players: if it's set to 4, it means 3 ghosts
                # If it's set lower, respect that but ensure at least 1 ghost
                max_players = max(1, min(3, max_players))

            # Create new game session with mode and difficulty
            new_game = GameSession(game_id, layout_name, max_players, game_mode, ai_difficulty)

            with self.games_lock:
                self.games[game_id] = new_game

            logger.info(f"Created new game: {game_id}, layout: {layout_name}, max players: {max_players}, mode: {game_mode}, AI difficulty: {ai_difficulty}")

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