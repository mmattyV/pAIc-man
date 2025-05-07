#!/usr/bin/env python3
"""
Fixed pAIcMan Client - A corrected implementation that works with the gRPC server
"""
import grpc
import uuid
import threading
import logging
import argparse
import sys
import os
import os.path
import time
import tkinter as tk
import tkinter.messagebox
from queue import Queue, Empty
from typing import List, Optional, Dict

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

# Import the pacman game components
from pacman import GameState
from helpers.game import Directions, AgentState, Configuration, Grid
from helpers.graphicsDisplay import PacmanGraphics
from helpers.layout import getLayout, tryToLoad

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/fixed_client.log", mode='a')
    ]
)
logger = logging.getLogger("fixed-client")

class GameStateAdapter:
    """Adapts gRPC GameState messages to Pacman GameState objects"""
    def __init__(self, layout_name):
        # Load the layout with more robust path checking
        self.layout = None
        layout_paths = [
            os.path.join('layouts', f"{layout_name}.lay"),  # layouts/name.lay
            f"{layout_name}.lay",  # name.lay directly
            os.path.join(os.path.dirname(__file__), 'layouts', f"{layout_name}.lay"),  # From current directory
            os.path.join(os.path.dirname(os.path.dirname(__file__)), 'layouts', f"{layout_name}.lay")  # From parent directory
        ]
        
        for path in layout_paths:
            if os.path.exists(path):
                self.layout = tryToLoad(path)
                if self.layout:
                    break
                    
        # If still not found, try getLayout as fallback
        if not self.layout:
            self.layout = getLayout(layout_name)
            
        if not self.layout:
            raise ValueError(f"Layout not found: {layout_name}")

        # Initialize a blank game state
        self.game_state = GameState()
        self.game_state.initialize(self.layout, self.layout.getNumGhosts())
        self.data = self.game_state.data

    def update_from_proto(self, proto_state):
        """Update the game state from a protobuf message"""
        try:
            # Update score
            self.data.score = proto_state.score

            # Clear food grid and update from proto
            width = self.layout.width
            height = self.layout.height

            # Store old food grid to detect changes
            old_food = self.data.food.copy() if self.data.food else None
            old_capsules = self.data.capsules[:] if self.data.capsules else []

            # Create new food grid from server data
            food_grid = Grid(width, height, False)
            for food_pos in proto_state.food:
                try:
                    x, y = int(food_pos.x), int(food_pos.y)
                    if 0 <= x < width and 0 <= y < height:
                        food_grid[x][y] = True
                except Exception as e:
                    logger.error(f"Error processing food position: {e}, pos: {food_pos}")

            # Find food that was eaten in this update
            if old_food:
                for x in range(width):
                    for y in range(height):
                        if old_food[x][y] and not food_grid[x][y]:
                            # Food was eaten at this position
                            self.data._foodEaten = (x, y)
                            logger.debug(f"Detected food eaten at: {x}, {y}")
                            break
            else:
                self.data._foodEaten = None

            self.data.food = food_grid

            # Update capsules and detect eaten capsules
            try:
                new_capsules = [(int(cap.x), int(cap.y)) for cap in proto_state.capsules]

                # Find capsule that was eaten
                if old_capsules:
                    for cap in old_capsules:
                        if cap not in new_capsules:
                            # Capsule was eaten
                            self.data._capsuleEaten = cap
                            logger.debug(f"Detected capsule eaten at: {cap}")
                            break
                else:
                    self.data._capsuleEaten = None

                self.data.capsules = new_capsules
            except Exception as e:
                logger.error(f"Error processing capsules: {e}")
                self.data.capsules = []
                self.data._capsuleEaten = None

            # Update agent states
            agent_states = []

            # Direction conversion map
            dir_map = {
                pacman_pb2.STOP: Directions.STOP,
                pacman_pb2.NORTH: Directions.NORTH,
                pacman_pb2.SOUTH: Directions.SOUTH,
                pacman_pb2.EAST: Directions.EAST,
                pacman_pb2.WEST: Directions.WEST
            }

            # Process agents
            for i, agent in enumerate(proto_state.agents):
                try:
                    # Create position and configuration
                    pos = (agent.position.x, agent.position.y)
                    direction = dir_map.get(agent.direction, Directions.STOP)
                    config = Configuration(pos, direction)

                    # Set agent state
                    is_pacman = agent.agent_type == pacman_pb2.PACMAN
                    agent_state = AgentState(config, is_pacman)

                    # Set scared timer for ghosts
                    if not is_pacman and agent.scared_timer > 0:
                        agent_state.scaredTimer = agent.scared_timer

                    agent_states.append(agent_state)
                except Exception as e:
                    logger.error(f"Error processing agent {i}: {e}, agent: {agent}")

            # Make sure we have the right number of agents
            while len(agent_states) < len(self.data.agentStates):
                if len(self.data.agentStates) > 0:
                    # Use the initial state for missing agents
                    agent_states.append(self.data.agentStates[len(agent_states)])
                else:
                    # If there are no initial agent states, break to avoid an infinite loop
                    break

            if agent_states:  # Only update if we have valid agent states
                # Track all moving agents
                moved_agents = []
                logger.debug(f"Comparing {len(self.data.agentStates)} old vs {len(agent_states)} new states.")

                # First identify the pacman and ghost indices
                pacman_idx = None
                ghost_indices = []

                for idx, agent in enumerate(agent_states):
                    if agent.isPacman:
                        pacman_idx = idx
                    else:
                        ghost_indices.append(idx)

                # Check if any ghosts moved - for AI Pacman mode, prioritize ghost movement
                for idx, (old, new) in enumerate(zip(self.data.agentStates, agent_states)):
                    pos_changed = old.getPosition() != new.getPosition()
                    dir_changed = old.getDirection() != new.getDirection()
                    scared_changed = (getattr(old, 'scaredTimer', 0) > 0) != (getattr(new, 'scaredTimer', 0) > 0)

                    # Log all positional or directional changes
                    if pos_changed or dir_changed:
                        moved_agents.append(idx)
                        logger.info(f"Detected move for agent index {idx}. Pos changed: {pos_changed}, Dir changed: {dir_changed}")
                        logger.debug(f"Old: {old.getPosition()} {old.getDirection()} | New: {new.getPosition()} {new.getDirection()}")

                        # If this is a ghost controlled by a human player, always prioritize its movement
                        if not old.isPacman and idx in ghost_indices:
                            moved_idx = idx

                    # If a ghost's scared state changes but position doesn't, still mark it as moved
                    elif not old.isPacman and scared_changed:
                        moved_agents.append(idx)
                        logger.info(f"Ghost at index {idx} scared state changed: {getattr(old, 'scaredTimer', 0)} -> {getattr(new, 'scaredTimer', 0)}")

                # In AI Pacman mode, prioritize ghost movement over AI Pacman movement
                # Set moved_idx to a ghost that moved, or pacman if no ghosts moved, or default to 0
                ghost_moves = [idx for idx in moved_agents if idx in ghost_indices]
                if ghost_moves:
                    moved_idx = ghost_moves[0]  # Prioritize ghost movements
                    logger.debug(f"Using ghost move at index {moved_idx}")
                elif moved_agents:
                    moved_idx = moved_agents[0]  # Fall back to any agent that moved
                    logger.debug(f"Using any agent move at index {moved_idx}")
                else:
                    moved_idx = 0  # Default

                # Add a log if no move was detected
                if not moved_agents and len(agent_states) > 0:
                    logger.warning(f"No agent move detected after checking {len(agent_states)} agents, defaulting moved_idx to {moved_idx}.")

                # Special debug for movement verification
                logger.debug(f"Final _agentMoved setting: {moved_idx} (found {len(moved_agents)} moves)")
                if moved_idx in ghost_indices:
                    logger.info(f"Movement focused on GHOST at index {moved_idx}")
                elif moved_idx == pacman_idx:
                    logger.info(f"Movement focused on PACMAN at index {moved_idx}")
                else:
                    logger.info(f"Movement focused on UNKNOWN agent type at index {moved_idx}")
                self.data.agentStates = agent_states
                self.data._agentMoved = moved_idx
                logger.debug(f"Setting _agentMoved to: {moved_idx}") # Log the final index being set

            return self.game_state
        except Exception as e:
            logger.error(f"Error updating game state: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return self.game_state

class FixedPacmanClient:
    """Fixed client implementation for pAIcMan game"""
    def __init__(self, server_address):
        self.server_address = server_address
        self.player_id = str(uuid.uuid4())[:8]  # Generate random player ID
        self.stub = None
        self.channel = None
        self.running = False
        self.connected = False
        self.game_id = None
        self.layout_name = None
        self.game_mode = pacman_pb2.PVP  # Default to PVP mode
        self.ai_difficulty = pacman_pb2.MEDIUM  # Default AI difficulty

        # Queues for communication
        self.action_queue = Queue()  # For sending actions to server
        self.game_state_queue = Queue()  # For receiving game states from server

        self.layout_name = "mediumClassic"
        self.failed_nodes = set()  # Track failed nodes to avoid quick retries
        self.intentional_leave = False  # Flag for intentional leave

        # Default cluster nodes - all potential leaders
        self.cluster_nodes = [
            "localhost:50051",
            "localhost:50052",
            "localhost:50053"
        ]

        # Connect to server
        self.connect()

    def connect(self):
        """Connect to the gRPC server"""
        try:
            logger.info(f"Connecting to server at {self.server_address}")
            self.channel = grpc.insecure_channel(self.server_address)
            self.stub = pacman_pb2_grpc.PacmanGameStub(self.channel)
            logger.info("Connected to server")
            self.connected = True
            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            self.failed_nodes.add(self.server_address)
            return False

    def reconnect_to_leader(self):
        """Try to reconnect to a different node in the cluster that might be the leader"""
        # Close current connection
        if self.channel:
            self.channel.close()
            self.channel = None
            self.stub = None
            self.connected = False
        
        # Mark the current node as failed
        self.failed_nodes.add(self.server_address)
        
        # Look for working node, preferably one that hasn't failed recently
        available_nodes = [node for node in self.cluster_nodes if node not in self.failed_nodes]
        
        # If all nodes have recently failed, try them all again
        if not available_nodes:
            available_nodes = self.cluster_nodes.copy()
            self.failed_nodes.clear()
        
        logger.info(f"Attempting to find new leader from available nodes: {available_nodes}")
        
        # Try each node until we find one that works
        for node in available_nodes:
            self.server_address = node
            if self.connect():
                logger.info(f"Reconnected to node {node}")
                return True
                
        logger.error("Failed to connect to any node in the cluster")
        return False

    def disconnect(self):
        """Disconnect from the server"""
        if self.running:
            self.leave_game()

        if self.channel:
            self.channel.close()
            self.channel = None
            self.stub = None
            logger.info("Disconnected from server")

    def create_game(self, layout_name="mediumClassic", max_players=4, game_mode=pacman_pb2.PVP, ai_difficulty=pacman_pb2.MEDIUM):
        """Create a new game on the server"""
        try:
            # Store mode and difficulty settings
            self.game_mode = game_mode
            self.ai_difficulty = ai_difficulty

            # Log what we're creating
            mode_str = "PVP" if game_mode == pacman_pb2.PVP else "AI Pacman"
            difficulty_str = ""
            if game_mode == pacman_pb2.AI_PACMAN:
                difficulty_map = {pacman_pb2.EASY: "Easy", pacman_pb2.MEDIUM: "Medium", pacman_pb2.HARD: "Hard"}
                difficulty_str = f" with {difficulty_map.get(ai_difficulty, 'Medium')} difficulty"

            logger.info(f"Creating {mode_str} game{difficulty_str} with layout {layout_name}")

            # Create game with specified mode and difficulty
            response = self.stub.CreateGame(
                pacman_pb2.GameConfig(
                    layout_name=layout_name,
                    max_players=max_players,
                    game_mode=game_mode,
                    ai_difficulty=ai_difficulty
                )
            )

            if response.success:
                self.game_id = response.game_id
                self.layout_name = layout_name
                logger.info(f"Created {mode_str} game{difficulty_str} with ID: {self.game_id}")
                return self.game_id
            else:
                logger.error(f"Failed to create game: {response.error_message}")
                return None

        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.FAILED_PRECONDITION and "leader" in str(e.details()).lower():
                logger.warning("Current node is not the leader, attempting to find leader...")
                if self.reconnect_to_leader():
                    # Retry with new leader, ensuring all params are passed
                    return self.create_game(layout_name, max_players, game_mode, ai_difficulty)
            # Log other RPC errors or if reconnect fails
            logger.error(f"Error creating game: {e}")
            return None
        except Exception as e:
            logger.error(f"Error creating game: {e}")
            return None

    def list_games(self):
        """List available games on the server"""
        try:
            response = self.stub.ListGames(pacman_pb2.Empty())
            logger.info(f"Found {len(response.games)} games")
            return response.games
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.FAILED_PRECONDITION and "leader" in str(e.details()).lower():
                logger.warning("Current node is not the leader, attempting to find leader...")
                if self.reconnect_to_leader():
                    # Retry with new leader
                    return self.list_games()
            logger.error(f"Error listing games: {e}")
            return []
        except Exception as e:
            logger.error(f"Error listing games: {e}")
            return []

    def join_game(self, game_id, layout_name="mediumClassic"):
        """Join a game by ID"""
        if self.running:
            logger.warning("Already in a game. Call leave_game() first.")
            return False

        try:
            self.game_id = game_id
            self.layout_name = layout_name
            self.running = True
            self.already_joined = False  # Reset join state for new game

            # Start game thread - will handle bidirectional streaming
            self.game_thread = threading.Thread(
                target=self._game_thread_func,
                daemon=True
            )
            self.game_thread.start()

            logger.info(f"Joined game {game_id}")
            return True
        except Exception as e:
            logger.error(f"Error joining game: {e}")
            self.running = False
            return False


    def _game_thread_func(self):
        """
        Handle bidirectional streaming with the server, including:
        • initial JOIN
        • automatic reconnect/leader failover with backoff
        • optional clean LEAVE when the user quits
        """
        retry_count = 0
        max_retries = 5  # Increased retries to handle more failure cases
        retry_delay = 1.0  # Start with 1 second delay
        self.already_joined = False  # Track if we've already joined the game
        self.intentional_leave = False  # Reset intentional leave flag
        
        while self.running and retry_count <= max_retries:
            try:
                # ----------------------------------------------------------
                # 1. generator that yields PlayerAction messages to server
                # ----------------------------------------------------------
                def generate_actions():
                    nonlocal retry_count, retry_delay
                    
                    # First action is always JOIN, unless we're reconnecting
                    if not self.already_joined:
                        join_action = pacman_pb2.PlayerAction(
                            player_id=self.player_id,
                            game_id=self.game_id,
                            action_type=pacman_pb2.JOIN
                        )
                        logger.info(f"Sending JOIN action for game {self.game_id}")
                        yield join_action
                        self.already_joined = True
                    else:
                        # If reconnecting, send a MOVE action with STOP direction
                        # This serves as a "ping" to get the current game state
                        logger.info(f"Reconnected to existing game {self.game_id}, resuming play")
                        resume_action = pacman_pb2.PlayerAction(
                            player_id=self.player_id,
                            game_id=self.game_id,
                            action_type=pacman_pb2.MOVE,
                            direction=pacman_pb2.STOP
                        )
                        yield resume_action

                    # Then yield actions from the queue
                    while self.running:
                        try:
                            action = self.action_queue.get(timeout=0.1)
                            logger.debug(f"Sending action: {action.action_type}")
                            yield action
                        except Empty:
                            # No action available, continue
                            continue
                        except Exception as e:
                            logger.error(f"Error generating actions: {e}")
                            break

                    # Only yield a LEAVE action when intentionally leaving the game
                    # Not when we're just reconnecting due to leader failover
                    if self.game_id and self.intentional_leave:
                        leave_action = pacman_pb2.PlayerAction(
                            player_id=self.player_id,
                            game_id=self.game_id,
                            action_type=pacman_pb2.LEAVE
                        )
                        logger.info(f"Sending LEAVE action for game {self.game_id}")
                        yield leave_action

                # ----------------------------------------------------------
                # 2. open the bidi stream
                # ----------------------------------------------------------
                stream = self.stub.PlayGame(generate_actions())

                # Process incoming game states
                for game_state in stream:
                    if not self.running:
                        break

                    # Put game state in queue for main thread to process
                    self.game_state_queue.put(game_state)

                    # Reset retry count on successful communication
                    retry_count = 0 
                    retry_delay = 1.0

                    # Check if game has ended
                    if game_state.status == pacman_pb2.FINISHED:
                        logger.info(f"Game {self.game_id} finished")
                        self.running = False
                        break

                # If we get here without errors, break the retry loop
                break
                
            # --------------------------------------------------------------
            # 3. error handling & leader fail‑over
            # --------------------------------------------------------------
            except grpc.RpcError as e:
                # Check for leader-related errors (both StatusCode.FAILED_PRECONDITION and StatusCode.UNAVAILABLE)
                if ((e.code() == grpc.StatusCode.FAILED_PRECONDITION and "leader" in str(e.details()).lower()) or
                    (e.code() == grpc.StatusCode.UNAVAILABLE)):
                    logger.warning(f"Connection error: {e}. Retrying in {retry_delay:.1f}s ({retry_count+1}/{max_retries})")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 1.5, 5.0)  # Less aggressive backoff with lower max
                    retry_count += 1
                    
                    # Try to reconnect to a different server
                    if self.reconnect_to_leader():
                        # If we successfully reconnected, reset retry count
                        retry_count = 0
                        retry_delay = 1.0
                        logger.info(f"Successfully reconnected to new leader at {self.server_address}, continuing game")
                        continue
                else:
                    # Other RPC errors
                    logger.error(f"RPC error: {e}")
                    self.running = False
                    break
                    
            except Exception as e:
                logger.error(f"Error in game thread: {e}")
                self.running = False
                break
                
        # End of retry loop
        if retry_count > max_retries:
            logger.error(f"Failed to connect after {max_retries} retries")
        
        logger.info(f"Game thread for {self.game_id} ended")

    def send_move(self, direction):
        """Send a move action to the server"""
        if not self.running:
            return False

        try:
            action = pacman_pb2.PlayerAction(
                player_id=self.player_id,
                game_id=self.game_id,
                action_type=pacman_pb2.MOVE,
                direction=direction
            )
            self.action_queue.put(action)
            return True
        except Exception as e:
            logger.error(f"Error sending move: {e}")
            return False

    # This method is not used - the PacmanGUI class handles display updates

    def play_game(self, game_id, choice):
        """Start playing the game with the selected role"""
        if not self.connected:
            logger.error("Not connected to server")
            return

        self.game_id = game_id
        self.running = True

        # Start game thread
        self.game_thread = threading.Thread(target=self._game_thread_func, args=(choice,))
        self.game_thread.daemon = True
        self.game_thread.start()

    def leave_game(self):
        """Leave the current game"""
        if not self.running:
            return

        # Flag that we're intentionally leaving
        self.intentional_leave = True
        self.running = False

        # Wait for game thread to end
        if hasattr(self, 'game_thread') and self.game_thread.is_alive():
            self.game_thread.join(timeout=2.0)

        logger.info(f"Left game {self.game_id}")
        self.game_id = None


class PacmanGUI:
    """GUI application for the Pacman client"""
    def __init__(self, root, server_address="localhost:50051"):
        self.root = root
        self.root.title("pAIcMan Client")

        # Initialize variables
        self.role_var = tk.StringVar(value="Role: None")
        self.status_var = tk.StringVar(value="Disconnected")
        self.game_info = {}  # Track game information for each listbox item
        self.display = None
        self.adapter = None

        # Initialize PacmanClient
        self.client = FixedPacmanClient(server_address)

        # Game mode and difficulty variables
        self.game_mode_var = tk.IntVar(value=pacman_pb2.PVP)
        self.ai_difficulty_var = tk.IntVar(value=pacman_pb2.MEDIUM)

        # Create UI elements
        self.build_ui()

        # Set up close handler
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Track connection status
        self.connection_status_label = tk.Label(self.root, text="Connected", fg="green")
        self.connection_status_label.pack(pady=5)

        # Start UI update loop
        self.update_ui()

    # ... rest of the class ...

    def update_ui(self):
        """Update the game UI with the latest state from the server"""
        # Update connection status
        if self.client.stub is None:
            self.connection_status_label.config(text="Disconnected", fg="red")
            # Try to reconnect if disconnected (removed game_id check for broader reconnection attempt)
            if self.client.reconnect_to_leader():
                self.connection_status_label.config(text=f"Connected to {self.client.server_address}", fg="green")
                logger.info(f"Reconnected to server at {self.client.server_address} via UI update")
        else:
            self.connection_status_label.config(text=f"Connected to {self.client.server_address}", fg="green")

        # Process game state updates
        if self.client.running and self.adapter:
            try:
                while not self.client.game_state_queue.empty():
                    proto_game_state = self.client.game_state_queue.get_nowait()
                    #print(f"Received state with {len(proto_game_state.agents)} agents, {len(proto_game_state.food)} food items")

                    # Detect food reset
                    is_food_reset = False
                    current_food_count = len(proto_game_state.food)
                    if hasattr(self, 'initial_food_count') and self.initial_food_count > 0: # Check if initialized
                        # A reset likely happened if the count was low and now matches initial
                        # Using a small threshold (e.g., <= 5) for 'low count'
                        if self.previous_food_count <= 5 and current_food_count == self.initial_food_count:
                            is_food_reset = True
                            logger.info(f"Food reset detected! Prev: {self.previous_food_count}, Curr: {current_food_count}, Initial: {self.initial_food_count}")

                    # Detect capsule reset
                    is_capsule_reset = False
                    current_capsule_count = len(proto_game_state.capsules)
                    if hasattr(self, 'initial_capsule_count') and self.initial_capsule_count > 0: # Check if initialized
                        # Capsule reset likely happened if count was 0 and now matches initial
                        if self.previous_capsule_count == 0 and current_capsule_count == self.initial_capsule_count:
                            is_capsule_reset = True
                            logger.info(f"Capsule reset detected! Prev: {self.previous_capsule_count}, Curr: {current_capsule_count}, Initial: {self.initial_capsule_count}")

                    # Find player role
                    for agent in proto_game_state.agents:
                        if agent.player_id == self.client.player_id:
                            role = "Pacman" if agent.agent_type == pacman_pb2.PACMAN else "Ghost"
                            self.role_var.set(f"Role: {role}")
                            break

                    pacman_state = self.adapter.update_from_proto(proto_game_state)

                    # Update the display
                    if is_food_reset:
                        logger.debug("Calling redrawFood()...")
                        # Redraw the food grid explicitly
                        self.display.redrawFood(pacman_state.data.food)
                        # Clear the _foodEaten flag to prevent immediate removal by standard update
                        pacman_state.data._foodEaten = None

                    if is_capsule_reset:
                        logger.debug("Calling redrawCapsules()...")
                        # Redraw the capsules explicitly
                        # Ensure pacman_state.data.capsules is the correct list format
                        self.display.redrawCapsules(pacman_state.data.capsules)
                        # Clear the _capsuleEaten flag
                        pacman_state.data._capsuleEaten = None

                    # Call the standard update method which handles agents, score, etc.
                    # If reset happened, it will now skip food/capsule removal due to cleared flags.
                    self.display.update(pacman_state.data)

                    # Update previous food/capsule count for next iteration's check
                    if hasattr(self, 'previous_food_count'): # Ensure attribute exists
                        self.previous_food_count = current_food_count
                    if hasattr(self, 'previous_capsule_count'):
                        self.previous_capsule_count = current_capsule_count

                    # Update status
                    self.status_var.set(f"Game: {self.client.game_id} | Score: {proto_game_state.score}")

                    # Check if game ended
                    if proto_game_state.status == pacman_pb2.FINISHED:
                        # Find if there's a pacman in the game
                        pacman_found = False
                        for agent in proto_game_state.agents:
                            if agent.agent_type == pacman_pb2.PACMAN:
                                pacman_found = True
                                break

                        # Determine game end message
                        message = "Game Over"
                        if not pacman_found:
                            message = "Pacman was eaten by a ghost!"

                        # Show game over dialog
                        tk.messagebox.showinfo(
                            "Game Over",
                            f"{message}\nFinal Score: {proto_game_state.score}"
                        )
                        self.leave_game()
                        break
            except Exception as e:
                logger.error(f"Error updating UI: {e}")

        # Schedule next update
        self.root.after(100, self.update_ui)  # 10 FPS for UI updates

    def build_ui(self):
        """Build the user interface"""
        # Main frame
        main_frame = tk.Frame(self.root, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Server settings
        server_frame = tk.LabelFrame(main_frame, text="Server", padx=5, pady=5)
        server_frame.pack(fill=tk.X, pady=5)

        tk.Label(server_frame, text="Address:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.server_var = tk.StringVar(value=self.client.server_address)
        tk.Entry(server_frame, textvariable=self.server_var, width=30).grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        tk.Button(server_frame, text="Connect", command=self.connect).grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)

        # Game creation
        create_frame = tk.LabelFrame(main_frame, text="Create Game", padx=5, pady=5)
        create_frame.pack(fill=tk.X, pady=5)

        # Layout selection
        tk.Label(create_frame, text="Layout:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.layout_var = tk.StringVar(value="mediumClassic")
        layouts = tk.OptionMenu(create_frame, self.layout_var,
                               "mediumClassic", "smallClassic", "minimaxClassic", "originalClassic")
        layouts.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        # Game mode selection
        mode_frame = tk.LabelFrame(create_frame, text="Game Mode", padx=5, pady=5)
        mode_frame.grid(row=1, column=0, columnspan=2, sticky=tk.W+tk.E, padx=5, pady=5)

        tk.Radiobutton(mode_frame, text="Player vs Player", variable=self.game_mode_var,
                       value=pacman_pb2.PVP, command=self.toggle_difficulty_display).pack(anchor=tk.W, padx=5, pady=2)
        tk.Radiobutton(mode_frame, text="AI Pacman", variable=self.game_mode_var,
                       value=pacman_pb2.AI_PACMAN, command=self.toggle_difficulty_display).pack(anchor=tk.W, padx=5, pady=2)

        # AI Difficulty selection
        self.difficulty_frame = tk.LabelFrame(create_frame, text="AI Difficulty", padx=5, pady=5)
        self.difficulty_frame.grid(row=2, column=0, columnspan=2, sticky=tk.W+tk.E, padx=5, pady=5)

        tk.Radiobutton(self.difficulty_frame, text="Easy", variable=self.ai_difficulty_var, value=pacman_pb2.EASY).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(self.difficulty_frame, text="Medium", variable=self.ai_difficulty_var, value=pacman_pb2.MEDIUM).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(self.difficulty_frame, text="Hard", variable=self.ai_difficulty_var, value=pacman_pb2.HARD).pack(side=tk.LEFT, padx=10)

        # Initially disable difficulty selection if PVP mode is selected
        self.toggle_difficulty_display()

        # Create game button
        tk.Button(create_frame, text="Create Game", command=self.create_game).grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=5, pady=10)

        # Game list
        games_frame = tk.LabelFrame(main_frame, text="Game List", padx=5, pady=5)
        games_frame.pack(fill=tk.X, pady=5)

        self.games_listbox = tk.Listbox(games_frame, height=5, width=80)
        self.games_listbox.pack(fill=tk.X, pady=5)

        # Buttons
        button_frame = tk.Frame(games_frame)
        button_frame.pack(fill=tk.X, pady=5)

        tk.Button(button_frame, text="Refresh", command=self.refresh_games).pack(side=tk.LEFT, padx=5)
        tk.Button(button_frame, text="Join Game", command=self.join_selected_game).pack(side=tk.LEFT, padx=5)

        # Status bar
        status_bar = tk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # Key bindings for movement
        self.root.bind("<Up>", lambda e: self.send_move(pacman_pb2.NORTH))
        self.root.bind("<Down>", lambda e: self.send_move(pacman_pb2.SOUTH))
        self.root.bind("<Left>", lambda e: self.send_move(pacman_pb2.WEST))
        self.root.bind("<Right>", lambda e: self.send_move(pacman_pb2.EAST))
        self.root.bind("<Escape>", lambda e: self.leave_game())
        self.root.bind("w", lambda e: self.send_move(pacman_pb2.NORTH))
        self.root.bind("s", lambda e: self.send_move(pacman_pb2.SOUTH))
        self.root.bind("a", lambda e: self.send_move(pacman_pb2.WEST))
        self.root.bind("d", lambda e: self.send_move(pacman_pb2.EAST))

    def connect(self):
        """Connect to the server"""
        address = self.server_var.get()
        self.client.disconnect()  # Close any existing connection

        # Update client's server address
        self.client.server_address = address

        # Connect to server
        if self.client.connect():
            self.status_var.set(f"Connected to {address}")
            self.refresh_games()
        else:
            self.status_var.set("Failed to connect")

    def toggle_difficulty_display(self):
        """Show or hide the difficulty frame based on game mode"""
        if self.game_mode_var.get() == pacman_pb2.AI_PACMAN:
            self.difficulty_frame.grid(row=2, column=0, columnspan=2, sticky=tk.W+tk.E, padx=5, pady=5)
        else:
            self.difficulty_frame.grid_remove()

    def create_game(self):
        """Create a new game"""
        layout = self.layout_var.get()
        game_mode = self.game_mode_var.get()
        ai_difficulty = self.ai_difficulty_var.get() if game_mode == pacman_pb2.AI_PACMAN else pacman_pb2.MEDIUM

        # Create game with selected options
        game_id = self.client.create_game(
            layout_name=layout,
            max_players=4,
            game_mode=game_mode,
            ai_difficulty=ai_difficulty
        )

        if game_id:
            mode_text = "AI Pacman" if game_mode == pacman_pb2.AI_PACMAN else "PVP"
            difficulty_text = ""
            if game_mode == pacman_pb2.AI_PACMAN:
                difficulty_map = {pacman_pb2.EASY: "Easy", pacman_pb2.MEDIUM: "Medium", pacman_pb2.HARD: "Hard"}
                difficulty_text = f" ({difficulty_map[ai_difficulty]})"

            self.status_var.set(f"Created {mode_text}{difficulty_text} game: {game_id}")
            self.refresh_games()
        else:
            self.status_var.set("Failed to create game")

    def refresh_games(self):
        """Refresh the list of available games"""
        # Clear listbox
        self.games_listbox.delete(0, tk.END)
        self.game_info.clear()

        # Get games list
        games = self.client.list_games()
        if not games:
            self.status_var.set("No games available or failed to fetch games")
            return

        # Display each game
        for i, game in enumerate(games):
            # Determine status text
            if game.status == pacman_pb2.WAITING:
                status_text = "Waiting"
            elif game.status == pacman_pb2.IN_PROGRESS:
                status_text = "In Progress"
            else:
                status_text = "Finished"

            # Get game mode and difficulty information
            mode_text = "PVP" if game.game_mode == pacman_pb2.PVP else "AI Pacman"
            difficulty_text = ""
            if game.game_mode == pacman_pb2.AI_PACMAN:
                if game.ai_difficulty == pacman_pb2.EASY:
                    difficulty_text = " (Easy)"
                elif game.ai_difficulty == pacman_pb2.MEDIUM:
                    difficulty_text = " (Medium)"
                elif game.ai_difficulty == pacman_pb2.HARD:
                    difficulty_text = " (Hard)"

            players_text = f"{game.current_players}/{game.max_players}"

            self.games_listbox.insert(
                tk.END,
                f"ID: {game.game_id} | Layout: {game.layout_name} | Mode: {mode_text}{difficulty_text} | Players: {players_text} | Status: {status_text}"
            )

            # Store game info for later use
            self.game_info[i] = {
                "id": game.game_id,
                "layout": game.layout_name,
                "mode": game.game_mode,
                "difficulty": game.ai_difficulty
            }

        self.status_var.set(f"Found {len(games)} games")

    def join_selected_game(self):
        """Join the selected game"""
        selection = self.games_listbox.curselection()
        if not selection:
            tkinter.messagebox.showerror("Error", "No game selected")
            return

        # Get the game ID from the selection
        game_info = self.games_listbox.get(selection[0])
        game_id = game_info.split("|")[0].strip().replace("ID: ", "")
        layout_name = game_info.split("|")[1].strip().replace("Layout: ", "")

        # Join the game
        self.join_game(game_id, layout_name)

    def join_game(self, game_id, layout_name):
        """Join a game and initialize display"""
        try:
            # Initialize game adapter
            self.adapter = GameStateAdapter(layout_name)

            # Initialize display (before joining the game)
            self.display = PacmanGraphics(1.0)
            self.display.initialize(self.adapter.game_state.data)
            self.root.focus_force()

            # Add a status label for player role
            self.role_var = tk.StringVar(value="Role: Unknown")
            role_label = tk.Label(self.root, textvariable=self.role_var)
            role_label.pack(side=tk.TOP)

            # Store initial food count and initialize previous count
            try:
                self.initial_food_count = sum(1 for row in self.adapter.layout.food for cell in row if cell)
                self.previous_food_count = self.initial_food_count
                logger.info(f"Initial food count for layout {layout_name}: {self.initial_food_count}")
                self.initial_capsule_count = len(self.adapter.layout.capsules)
                self.previous_capsule_count = self.initial_capsule_count
                logger.info(f"Initial capsule count for layout {layout_name}: {self.initial_capsule_count}")
            except Exception as e:
                logger.error(f"Could not calculate initial food/capsule count: {e}")
                self.initial_food_count = -1 # Indicate error
                self.previous_food_count = -1
                self.initial_capsule_count = -1
                self.previous_capsule_count = -1

            # Join the game (this starts the streaming)
            if self.client.join_game(game_id, layout_name):
                self.status_var.set(f"Joined game {game_id}")
            else:
                self.status_var.set("Failed to join game")
                self.display = None
                self.adapter = None

        except Exception as e:
            logger.error(f"Error joining game: {e}")
            self.status_var.set(f"Error: {str(e)}")
            self.display = None
            self.adapter = None

    def send_move(self, direction):
        """Send a move command to the server"""
        if self.client.running:
            # Get the player's current role
            current_role = self.role_var.get() if hasattr(self, 'role_var') else "Unknown"

            # Log movement with role information
            logger.info(f"Sending {current_role} move: {direction}")

            # Send the move to the server
            self.client.send_move(direction)

    def leave_game(self):
        """Leave the current game"""
        if self.client.running:
            self.client.leave_game()

            # Clean up display
            self.display = None
            self.adapter = None

            self.status_var.set("Left game")

    def update_ui(self):
        """Update the game UI with the latest state from the server"""
        if self.adapter:
            try:
                while not self.client.game_state_queue.empty():
                    proto_game_state = self.client.game_state_queue.get_nowait()
                    #print(f"Received state with {len(proto_game_state.agents)} agents, {len(proto_game_state.food)} food items")

                    # Detect food reset
                    is_food_reset = False
                    current_food_count = len(proto_game_state.food)
                    if hasattr(self, 'initial_food_count') and self.initial_food_count > 0: # Check if initialized
                        # A reset likely happened if the count was low and now matches initial
                        # Using a small threshold (e.g., <= 5) for 'low count'
                        if self.previous_food_count <= 5 and current_food_count == self.initial_food_count:
                            is_food_reset = True
                            logger.info(f"Food reset detected! Prev: {self.previous_food_count}, Curr: {current_food_count}, Initial: {self.initial_food_count}")

                    # Detect capsule reset
                    is_capsule_reset = False
                    current_capsule_count = len(proto_game_state.capsules)
                    if hasattr(self, 'initial_capsule_count') and self.initial_capsule_count > 0: # Check if initialized
                        # Capsule reset likely happened if count was 0 and now matches initial
                        if self.previous_capsule_count == 0 and current_capsule_count == self.initial_capsule_count:
                            is_capsule_reset = True
                            logger.info(f"Capsule reset detected! Prev: {self.previous_capsule_count}, Curr: {current_capsule_count}, Initial: {self.initial_capsule_count}")

                    # Find player role
                    for agent in proto_game_state.agents:
                        if agent.player_id == self.client.player_id:
                            role = "Pacman" if agent.agent_type == pacman_pb2.PACMAN else "Ghost"
                            self.role_var.set(f"Role: {role}")
                            break

                    pacman_state = self.adapter.update_from_proto(proto_game_state)

                    # Update the display
                    if is_food_reset:
                        logger.debug("Calling redrawFood()...")
                        # Redraw the food grid explicitly
                        self.display.redrawFood(pacman_state.data.food)
                        # Clear the _foodEaten flag to prevent immediate removal by standard update
                        pacman_state.data._foodEaten = None

                    if is_capsule_reset:
                        logger.debug("Calling redrawCapsules()...")
                        # Redraw the capsules explicitly
                        # Ensure pacman_state.data.capsules is the correct list format
                        self.display.redrawCapsules(pacman_state.data.capsules)
                        # Clear the _capsuleEaten flag
                        pacman_state.data._capsuleEaten = None

                    # Call the standard update method which handles agents, score, etc.
                    # If reset happened, it will now skip food/capsule removal due to cleared flags.
                    self.display.update(pacman_state.data)

                    # Update previous food/capsule count for next iteration's check
                    if hasattr(self, 'previous_food_count'): # Ensure attribute exists
                        self.previous_food_count = current_food_count
                    if hasattr(self, 'previous_capsule_count'):
                        self.previous_capsule_count = current_capsule_count

                    # Update status
                    self.status_var.set(f"Game: {self.client.game_id} | Score: {proto_game_state.score}")

                    # Check if game ended
                    if proto_game_state.status == pacman_pb2.FINISHED:
                        # Find if there's a pacman in the game
                        pacman_found = False
                        for agent in proto_game_state.agents:
                            if agent.agent_type == pacman_pb2.PACMAN:
                                pacman_found = True
                                break

                        # Determine game end message and result type
                        if not pacman_found:
                            # Pacman was eaten by a ghost - Game Lost
                            title = "Game Over - You Lost!"
                            message = "Pacman was eaten by a ghost!"
                            icon = "warning"
                        elif current_food_count == 0:
                            # All food pellets eaten - Game Won
                            title = "Game Over - You Won!"
                            message = "Congratulations! Pacman ate all the food pellets!"
                            icon = "info"
                        else:
                            # Other game end condition
                            title = "Game Over"
                            message = "The game has ended."
                            icon = "info"

                        # Add game mode info to the message
                        game_mode = "AI Pacman" if proto_game_state.game_mode == pacman_pb2.AI_PACMAN else "PVP"

                        # Show enhanced game over dialog
                        tkinter.messagebox.showinfo(
                            title,
                            f"{message}\n\nGame Mode: {game_mode}\nFinal Score: {proto_game_state.score}"
                        )
                        self.leave_game()
                        break
            except Exception as e:
                logger.error(f"Error updating UI: {e}")

        # Schedule next update
        self.root.after(16, self.update_ui)  # Approx 60 FPS

    def on_close(self):
        """Handle window close event"""
        if self.client.running:
            self.client.leave_game()

        self.client.disconnect()
        self.root.destroy()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="pAIcMan Fixed Client")
    parser.add_argument("--server", type=str, default="localhost:50051", help="Server address")

    args = parser.parse_args()

    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)

    # Create and run the Tkinter application
    root = tk.Tk()
    root.geometry("800x600")
    app = PacmanGUI(root, args.server)
    root.mainloop()


if __name__ == "__main__":
    main()