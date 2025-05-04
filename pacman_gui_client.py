"""
pAIcMan GUI Client - Connects to the server via gRPC and displays the game with graphics
"""
import grpc
import uuid
import threading
import logging
import argparse
import sys
import os
import time
from typing import Dict, List, Optional
from queue import Queue

# Import generated protocol buffer code
import pacman_pb2
import pacman_pb2_grpc

# Import the original pacman game components
from pacman import GameState, ClassicGameRules
from helpers.game import Game, GameStateData, AgentState, Configuration, Grid
from helpers.game import Directions
import helpers.graphicsDisplay as graphicsDisplay
import helpers.layout as layout

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/gui_client.log", mode='a')
    ]
)
logger = logging.getLogger("pacman-gui-client")

class PacmanGameAdapter:
    """Adapts gRPC GameState messages to Pacman GameState objects"""

    def __init__(self, layout_name="mediumClassic"):
        # Load the layout
        self.layout = layout.getLayout(layout_name)
        if not self.layout:
            raise Exception(f"Could not find layout: {layout_name}")

        # Initialize a blank game state
        self.game_state = GameState()
        self.game_state.initialize(self.layout, 4)  # 4 = max ghosts
        self.data = self.game_state.data

    def update_from_grpc(self, grpc_state: pacman_pb2.GameState) -> GameState:
        """Update game state from gRPC message"""
        # Update score
        self.data.score = grpc_state.score

        # Clear existing food - we'll add what the server tells us
        height = self.layout.height
        width = self.layout.width
        self.data.food = Grid(width, height, False)

        # Add food pellets
        for food_pos in grpc_state.food:
            self.data.food[int(food_pos.x)][int(food_pos.y)] = True

        # Update capsules
        self.data.capsules = [(int(cap.x), int(cap.y)) for cap in grpc_state.capsules]

        # Update agent states
        self.data.agentStates = []
        for agent in grpc_state.agents:
            # Create agent configuration
            pos = (agent.position.x, agent.position.y)

            # Convert direction enum from gRPC to Pacman direction string
            dir_map = {
                pacman_pb2.STOP: Directions.STOP,
                pacman_pb2.NORTH: Directions.NORTH,
                pacman_pb2.SOUTH: Directions.SOUTH,
                pacman_pb2.EAST: Directions.EAST,
                pacman_pb2.WEST: Directions.WEST
            }
            direction = dir_map.get(agent.direction, Directions.STOP)

            config = Configuration(pos, direction)

            # Create agent state
            is_pacman = (agent.agent_type == pacman_pb2.PACMAN)
            agent_state = AgentState(config, is_pacman)

            # Set scared timer for ghosts
            if not is_pacman:
                agent_state.scaredTimer = agent.scared_timer

            self.data.agentStates.append(agent_state)

        return self.game_state

class PacmanGUIClient:
    """Client for the pAIcMan game with GUI display"""

    def __init__(self, server_address: str):
        self.server_address = server_address
        self.player_id = str(uuid.uuid4())[:8]  # Short unique ID for this player
        self.channel = None
        self.stub = None
        self.current_game_id = None
        self.game_state_queue = Queue()
        self.running = False

        # GUI components
        self.display = None
        self.game_adapter = None

        logger.info(f"PacmanGUIClient initialized with player_id: {self.player_id}")

    def connect(self):
        """Connect to the gRPC server"""
        try:
            logger.info(f"Connecting to server at {self.server_address}")
            self.channel = grpc.insecure_channel(self.server_address)
            self.stub = pacman_pb2_grpc.PacmanGameStub(self.channel)
            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}", exc_info=True)
            return False

    def disconnect(self):
        """Disconnect from the server"""
        if self.channel:
            self.channel.close()
            self.channel = None
            self.stub = None
            logger.info("Disconnected from server")

    def list_games(self) -> List[pacman_pb2.GameInfo]:
        """List available games on the server"""
        try:
            response = self.stub.ListGames(pacman_pb2.Empty())
            logger.info(f"Found {len(response.games)} available games")
            return response.games
        except Exception as e:
            logger.error(f"Error listing games: {e}", exc_info=True)
            return []

    def create_game(self, layout_name: str, max_players: int = 4) -> Optional[str]:
        """Create a new game on the server"""
        try:
            config = pacman_pb2.GameConfig(
                layout_name=layout_name,
                max_players=max_players
            )
            response = self.stub.CreateGame(config)

            if response.success:
                logger.info(f"Created game with ID: {response.game_id}")
                return response.game_id
            else:
                logger.error(f"Failed to create game: {response.error_message}")
                return None
        except Exception as e:
            logger.error(f"Error creating game: {e}", exc_info=True)
            return None

    def process_game_states(self):
        """Process incoming game states for network communication - runs in a separate thread"""
        try:
            for game_state in self.action_stream:
                # Add the received game state to the queue for the main thread to render
                self.game_state_queue.put(game_state)

                # If game is over, break out of the loop
                if game_state.status == pacman_pb2.FINISHED:
                    logger.info(f"Game {self.current_game_id} has ended. Winner: {game_state.winner_id}")
                    self.running = False
                    break
        except Exception as e:
            logger.error(f"Error processing game states: {e}", exc_info=True)
            self.running = False

    def join_game(self, game_id: str, layout_name: str = "mediumClassic") -> bool:
        """Join an existing game and start streaming with GUI"""
        try:
            self.current_game_id = game_id
            self.layout_name = layout_name

            # Create a queue for sending actions to the server
            from queue import Queue
            self.action_stream_queue = Queue()

            # Put initial JOIN action in the queue
            join_action = pacman_pb2.PlayerAction(
                player_id=self.player_id,
                game_id=game_id,
                action_type=pacman_pb2.JOIN
            )
            self.action_stream_queue.put(join_action)

            # Create a request iterator that will yield actions from our queue
            def request_iterator():
                while self.running or not self.action_stream_queue.empty():
                    try:
                        action = self.action_stream_queue.get(timeout=0.1)
                        yield action
                    except:
                        # If queue.get times out, just continue
                        continue

            # Create a bidirectional streaming RPC with our request iterator
            self.running = True  # Set this before creating the stream
            self.action_stream = self.stub.PlayGame(request_iterator())

            # Start game state processing in a separate thread
            self.game_state_thread = threading.Thread(
                target=self.process_game_states
            )
            self.game_state_thread.daemon = True
            self.game_state_thread.start()

            logger.info(f"Joined game {game_id} with GUI")
            return True
        except Exception as e:
            logger.error(f"Error joining game: {e}", exc_info=True)
            self.running = False
            return False

    def run_game(self):
        """Run the game loop with GUI on the main thread"""
        try:
            # Initialize the game adapter and display (on main thread)
            self.game_adapter = PacmanGameAdapter(self.layout_name)
            self.display = graphicsDisplay.PacmanGraphics()

            # Initialize with a blank state
            initial_game_state = self.game_adapter.game_state
            self.display.initialize(initial_game_state.data)

            # Process game states as they arrive (on main thread)
            while self.running:
                try:
                    # Non-blocking check for game states
                    try:
                        grpc_state = self.game_state_queue.get_nowait()

                        # Convert gRPC state to Pacman game state
                        game_state = self.game_adapter.update_from_grpc(grpc_state)

                        # Update the display
                        self.display.update(game_state.data)
                    except queue.Empty:
                        pass

                    # Process keyboard input (non-blocking)
                    self.check_keyboard_input()

                    # Small sleep to prevent CPU hogging
                    time.sleep(0.05)

                except Exception as e:
                    if self.running:
                        logger.error(f"Error in game loop: {e}", exc_info=True)
                    else:
                        break
        finally:
            # Clean up display
            if hasattr(self, 'display') and self.display:
                self.display.finish()

    def leave_game(self):
        """Leave the current game"""
        if self.current_game_id:
            # Send LEAVE action
            leave_action = pacman_pb2.PlayerAction(
                player_id=self.player_id,
                game_id=self.current_game_id,
                action_type=pacman_pb2.LEAVE
            )
            self.action_stream_queue.put(leave_action)

            # Stop the game loop
            self.running = False

            # Wait for thread to finish
            if self.game_state_thread:
                self.game_state_thread.join(timeout=1.0)

            self.current_game_id = None
            logger.info("Left the game")

    def check_keyboard_input(self):
        """Non-blocking keyboard input check"""
        try:
            import select
            import termios
            import tty

            # Check if there's input available without blocking
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                # Save terminal settings
                old_settings = termios.tcgetattr(sys.stdin)
                try:
                    # Set terminal to raw mode
                    tty.setraw(sys.stdin.fileno())
                    key = sys.stdin.read(1)

                    if key == 'q':
                        self.leave_game()
                    elif key == '\x1b':  # Escape sequence for arrow keys
                        # Read the next two characters
                        sys.stdin.read(1)  # Skip the [
                        arrow = sys.stdin.read(1)

                        direction = None
                        if arrow == 'A':  # Up
                            direction = pacman_pb2.NORTH
                        elif arrow == 'B':  # Down
                            direction = pacman_pb2.SOUTH
                        elif arrow == 'C':  # Right
                            direction = pacman_pb2.EAST
                        elif arrow == 'D':  # Left
                            direction = pacman_pb2.WEST

                        if direction is not None:
                            self.send_move(direction)
                    # WASD controls
                    elif key.lower() == 'w':
                        self.send_move(pacman_pb2.NORTH)
                    elif key.lower() == 's':
                        self.send_move(pacman_pb2.SOUTH)
                    elif key.lower() == 'd':
                        self.send_move(pacman_pb2.EAST)
                    elif key.lower() == 'a':
                        self.send_move(pacman_pb2.WEST)
                finally:
                    # Restore terminal settings
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except Exception as e:
            # Just log and continue if there's an error with keyboard handling
            logger.debug(f"Error checking keyboard input: {e}")

    def send_move(self, direction: int):
        """Send a move action to the server"""
        if self.current_game_id and self.running:
            move_action = pacman_pb2.PlayerAction(
                player_id=self.player_id,
                game_id=self.current_game_id,
                action_type=pacman_pb2.MOVE,
                direction=direction
            )
            self.action_stream_queue.put(move_action)
            logger.debug(f"Sent move: {direction}")

def handle_keyboard_input(client):
    """Handle keyboard input for the GUI client"""
    try:
        import termios
        import tty

        # Save terminal settings
        old_settings = termios.tcgetattr(sys.stdin)

        # Set terminal to raw mode
        tty.setraw(sys.stdin.fileno())

        while client.running:
            # Check if there's input available
            import select
            if select.select([sys.stdin], [], [], 0.1)[0]:
                key = sys.stdin.read(1)

                if key == 'q':
                    client.leave_game()
                    break
                elif key == '\x1b':  # Escape sequence for arrow keys
                    # Read the next two characters
                    sys.stdin.read(1)  # Skip the [
                    arrow = sys.stdin.read(1)

                    direction = None
                    if arrow == 'A':  # Up
                        direction = pacman_pb2.NORTH
                    elif arrow == 'B':  # Down
                        direction = pacman_pb2.SOUTH
                    elif arrow == 'C':  # Right
                        direction = pacman_pb2.EAST
                    elif arrow == 'D':  # Left
                        direction = pacman_pb2.WEST

                    if direction is not None:
                        client.send_move(direction)
                # WASD controls
                elif key.lower() == 'w':
                    client.send_move(pacman_pb2.NORTH)
                elif key.lower() == 's':
                    client.send_move(pacman_pb2.SOUTH)
                elif key.lower() == 'd':
                    client.send_move(pacman_pb2.EAST)
                elif key.lower() == 'a':
                    client.send_move(pacman_pb2.WEST)

    except Exception as e:
        logger.error(f"Error in keyboard handler: {e}", exc_info=True)
    finally:
        # Restore terminal settings
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except:
            pass

def gui_client_demo():
    """Interactive CLI for the GUI client"""
    parser = argparse.ArgumentParser(description="pAIcMan GUI Client")
    parser.add_argument("--server", type=str, default="localhost:50051", help="Server address in format host:port")

    args = parser.parse_args()

    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)

    # Create client and connect to server
    client = PacmanGUIClient(args.server)

    if not client.connect():
        print("Failed to connect to server. Exiting.")
        sys.exit(1)

    # Simple menu
    while True:
        print("\n=== pAIcMan GUI Client ===")
        print("1. List available games")
        print("2. Create new game")
        print("3. Join a game")
        print("4. Exit")

        choice = input("Enter choice (1-4): ")

        if choice == "1":
            games = client.list_games()
            if games:
                print("\nAvailable games:")
                for i, game in enumerate(games):
                    status_str = "Waiting" if game.status == pacman_pb2.WAITING else \
                                 "In Progress" if game.status == pacman_pb2.IN_PROGRESS else "Finished"
                    print(f"{i+1}. ID: {game.game_id}, Layout: {game.layout_name}, Players: {game.current_players}/{game.max_players}, Status: {status_str}")
            else:
                print("No games available")

        elif choice == "2":
            layout = input("Enter layout name (mediumClassic): ")
            if not layout:
                layout = "mediumClassic"

            try:
                max_players = int(input("Enter max players (4): ") or "4")
            except:
                max_players = 4

            game_id = client.create_game(layout, max_players)
            if game_id:
                print(f"Created game with ID: {game_id}")

        elif choice == "3":
            game_id = input("Enter game ID to join: ")
            if game_id:
                layout = input("Enter layout name (mediumClassic): ")
                if not layout:
                    layout = "mediumClassic"

                print(f"Joining game {game_id}. Use arrow keys or WASD to move. Press 'q' to quit.")

                # Initialize the game (this doesn't block, just sets up the streaming)
                if client.join_game(game_id, layout):
                    # Run the game on the main thread (this will block until game ends)
                    client.run_game()
                else:
                    print("Failed to join game")

        elif choice == "4":
            print("Exiting...")
            break

        else:
            print("Invalid choice")

    # Clean up
    client.disconnect()

if __name__ == "__main__":
    gui_client_demo()
