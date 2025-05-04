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
import time
import tkinter as tk
from queue import Queue, Empty
from typing import List, Optional, Dict

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

# Import the pacman game components
from pacman import GameState
from helpers.game import Directions, AgentState, Configuration, Grid
from helpers.graphicsDisplay import PacmanGraphics
from helpers.layout import getLayout

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
        # Load the layout
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
            food_grid = Grid(width, height, False)

            for food_pos in proto_state.food:
                try:
                    x, y = int(food_pos.x), int(food_pos.y)
                    if 0 <= x < width and 0 <= y < height:
                        food_grid[x][y] = True
                except Exception as e:
                    logger.error(f"Error processing food position: {e}, pos: {food_pos}")

            self.data.food = food_grid

            # Update capsules
            try:
                self.data.capsules = [(int(cap.x), int(cap.y)) for cap in proto_state.capsules]
            except Exception as e:
                logger.error(f"Error processing capsules: {e}")
                self.data.capsules = []

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
                moved_idx = 0 # Default to pacman if no move detected? Or keep previous? Let's default 0 for now.
                logger.debug(f"Comparing {len(self.data.agentStates)} old vs {len(agent_states)} new states.")
                for idx, (old, new) in enumerate(zip(self.data.agentStates, agent_states)):
                    pos_changed = old.getPosition() != new.getPosition()
                    dir_changed = old.getDirection() != new.getDirection()
                    if pos_changed or dir_changed:
                        moved_idx = idx
                        logger.info(f"Detected move for agent index {idx}. Pos changed: {pos_changed}, Dir changed: {dir_changed}")
                        logger.debug(f"Old: {old.getPosition()} {old.getDirection()} | New: {new.getPosition()} {new.getDirection()}")
                        break
                # Add a log if no move was detected after the loop
                if moved_idx == 0 and idx > 0: # Check if we defaulted after iterating
                    logger.warning(f"No agent move detected after checking {idx+1} agents, defaulting moved_idx to 0.")
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
        self.channel = None
        self.stub = None
        self.game_id = None
        self.action_queue = Queue()
        self.game_state_queue = Queue()
        self.running = False
        self.layout_name = "mediumClassic"

        # Connect to server
        self.connect()

    def connect(self):
        """Connect to the gRPC server"""
        try:
            logger.info(f"Connecting to server at {self.server_address}")
            self.channel = grpc.insecure_channel(self.server_address)
            self.stub = pacman_pb2_grpc.PacmanGameStub(self.channel)
            logger.info("Connected to server")
            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
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

    def create_game(self, layout_name="mediumClassic", max_players=4):
        """Create a new game on the server"""
        try:
            logger.info(f"Creating game with layout {layout_name}")
            response = self.stub.CreateGame(
                pacman_pb2.GameConfig(
                    layout_name=layout_name,
                    max_players=max_players
                )
            )

            if response.success:
                self.game_id = response.game_id
                self.layout_name = layout_name
                logger.info(f"Created game with ID: {self.game_id}")
                return self.game_id
            else:
                logger.error(f"Failed to create game: {response.error_message}")
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
        """Handle bidirectional streaming with the server"""
        try:
            # Create action generator that will yield player actions
            def generate_actions():
                # First action is always JOIN
                join_action = pacman_pb2.PlayerAction(
                    player_id=self.player_id,
                    game_id=self.game_id,
                    action_type=pacman_pb2.JOIN
                )
                logger.info(f"Sending JOIN action for game {self.game_id}")
                yield join_action

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

                # Always yield a LEAVE action when done
                if self.game_id:
                    leave_action = pacman_pb2.PlayerAction(
                        player_id=self.player_id,
                        game_id=self.game_id,
                        action_type=pacman_pb2.LEAVE
                    )
                    logger.info(f"Sending LEAVE action for game {self.game_id}")
                    yield leave_action

            # Start bidirectional streaming
            stream = self.stub.PlayGame(generate_actions())

            # Process incoming game states
            for game_state in stream:
                if not self.running:
                    break

                # Put game state in queue for main thread to process
                self.game_state_queue.put(game_state)

                # Check if game has ended
                if game_state.status == pacman_pb2.FINISHED:
                    logger.info(f"Game {self.game_id} finished")
                    self.running = False
                    break

        except Exception as e:
            logger.error(f"Error in game thread: {e}")
        finally:
            self.running = False
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

    def leave_game(self):
        """Leave the current game"""
        if not self.running:
            return

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
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Client for server communication
        self.client = FixedPacmanClient(server_address)

        # Game display
        self.display = None
        self.adapter = None

        # Build the UI
        self.build_ui()

        # Start UI update loop
        self.update_ui()

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

        tk.Label(create_frame, text="Layout:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
        self.layout_var = tk.StringVar(value="mediumClassic")
        layouts = tk.OptionMenu(create_frame, self.layout_var,
                               "mediumClassic", "smallClassic", "minimaxClassic", "originalClassic")
        layouts.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
        tk.Button(create_frame, text="Create Game", command=self.create_game).grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)

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
        self.status_var = tk.StringVar(value="Disconnected")
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

    def create_game(self):
        """Create a new game"""
        layout = self.layout_var.get()
        game_id = self.client.create_game(layout)

        if game_id:
            self.status_var.set(f"Created game: {game_id}")
            self.refresh_games()
        else:
            self.status_var.set("Failed to create game")

    def refresh_games(self):
        """Refresh the list of available games"""
        # Clear the existing items
        self.games_listbox.delete(0, tk.END)

        games = self.client.list_games()

        for game in games:
            status_text = "Waiting"
            if game.status == pacman_pb2.IN_PROGRESS:
                status_text = "In Progress"
            elif game.status == pacman_pb2.FINISHED:
                status_text = "Finished"

            players_text = f"{game.current_players}/{game.max_players}"

            self.games_listbox.insert(
                tk.END,
                f"ID: {game.game_id} | Layout: {game.layout_name} | Players: {players_text} | Status: {status_text}"
            )

        self.status_var.set(f"Found {len(games)} games")

    def join_selected_game(self):
        """Join the selected game"""
        selection = self.games_listbox.curselection()
        if not selection:
            tk.messagebox.showerror("Error", "No game selected")
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
        if self.client.running and self.adapter:
            try:
                while not self.client.game_state_queue.empty():
                    game_state = self.client.game_state_queue.get_nowait()
                    print(f"Received state with {len(game_state.agents)} agents, {len(game_state.food)} food items")

                    for agent in game_state.agents:
                        if agent.player_id == self.client.player_id:
                            role = "Pacman" if agent.agent_type == pacman_pb2.PACMAN else "Ghost"
                            self.role_var.set(f"Role: {role}")
                            break

                    # Convert to Pacman format
                    pacman_state = self.adapter.update_from_proto(game_state)

                    # Update the display
                    self.display.update(pacman_state.data)

                    # Update status
                    self.status_var.set(f"Game: {self.client.game_id} | Score: {game_state.score}")

                    # Check if game ended
                    if game_state.status == pacman_pb2.FINISHED:
                        winner = game_state.winner_id if game_state.winner_id else "Unknown"
                        tk.messagebox.showinfo("Game Over", f"Game ended. Winner: {winner}. Score: {game_state.score}")
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
