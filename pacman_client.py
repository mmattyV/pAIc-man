"""
pAIcMan Client - Connects to the server via gRPC and handles game interactions
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/client.log", mode='a')
    ]
)
logger = logging.getLogger("pacman-client")

class PacmanClient:
    """Client for the pAIcMan game"""

    def __init__(self, server_address: str):
        self.server_address = server_address
        self.player_id = str(uuid.uuid4())[:8]  # Short unique ID for this player
        self.channel = None
        self.stub = None
        self.current_game_id = None
        self.game_state_queue = Queue()
        # We'll initialize action_stream_queue when joining a game
        self.game_state_thread = None
        self.action_thread = None
        self.running = False

        logger.info(f"PacmanClient initialized with player_id: {self.player_id}")

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

    def _game_state_handler(self, game_state_stream):
        """Process incoming game states from the server"""
        try:
            for game_state in game_state_stream:
                # Add the received game state to the queue for rendering
                self.game_state_queue.put(game_state)

                # Check if game is over
                if game_state.status == pacman_pb2.FINISHED:
                    logger.info(f"Game {self.current_game_id} has ended. Winner: {game_state.winner_id}")
                    self.running = False
                    break
        except Exception as e:
            logger.error(f"Error in game state stream: {e}", exc_info=True)
            self.running = False

    def _action_sender(self, action_stream):
        """This method is no longer needed with our new approach"""
        # The action sending is now handled by the request_iterator in join_game
        # This method is kept as a placeholder for compatibility
        pass

    def join_game(self, game_id: str) -> bool:
        """Join an existing game and start streaming"""
        try:
            self.current_game_id = game_id
            
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
            action_stream = self.stub.PlayGame(request_iterator())

            # Start the game state handler thread
            self.running = True
            self.game_state_thread = threading.Thread(
                target=self._game_state_handler,
                args=(action_stream,)
            )
            self.game_state_thread.daemon = True
            self.game_state_thread.start()

            # Start the action sender thread
            self.action_thread = threading.Thread(
                target=self._action_sender,
                args=(action_stream,)
            )
            self.action_thread.daemon = True
            self.action_thread.start()

            logger.info(f"Joined game {game_id}")
            return True
        except Exception as e:
            logger.error(f"Error joining game: {e}", exc_info=True)
            return False

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
            
            # Wait for threads to finish
            if self.game_state_thread:
                self.game_state_thread.join(timeout=1.0)
            if self.action_thread:
                self.action_thread.join(timeout=1.0)
                
            self.current_game_id = None
            logger.info("Left the game")

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

def simple_text_client_demo():
    """A simple text-based demo of the client"""
    parser = argparse.ArgumentParser(description="pAIcMan Game Client")
    parser.add_argument("--server", type=str, default="localhost:50051", help="Server address in format host:port")

    args = parser.parse_args()

    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)

    # Create client and connect to server
    client = PacmanClient(args.server)

    if not client.connect():
        print("Failed to connect to server. Exiting.")
        sys.exit(1)

    # Simple menu
    while True:
        print("\n=== pAIcMan Client ===")
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
            layout = input("Enter layout name (default): ")
            if not layout:
                layout = "default"

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
                if client.join_game(game_id):
                    print(f"Joined game {game_id}. Use arrow keys to move.")
                    print("Press 'q' to quit the game.")

                    # Simple game loop
                    try:
                        while client.running:
                            # Check for game state updates
                            try:
                                game_state = client.game_state_queue.get(timeout=0.1)
                                # Display game state (this would be replaced by UI rendering)
                                print(f"\rScore: {game_state.score} | Agents: {len(game_state.agents)}", end="")
                            except:
                                pass

                            # Check for key input (very simplified)
                            if not sys.stdin.isatty():
                                # Can't read input in non-interactive mode
                                time.sleep(0.1)
                                continue

                            import termios, tty, select

                            # Set terminal to raw mode
                            old_settings = termios.tcgetattr(sys.stdin)
                            try:
                                tty.setraw(sys.stdin.fileno())

                                # Check if there's input available
                                if select.select([sys.stdin], [], [], 0)[0]:
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
                            finally:
                                # Restore terminal settings
                                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                    except Exception as e:
                        logger.error(f"Error in game loop: {e}", exc_info=True)
                    finally:
                        # Make sure we leave the game
                        client.leave_game()
                        # Restore terminal settings
                        try:
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        except:
                            pass
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
    simple_text_client_demo()
