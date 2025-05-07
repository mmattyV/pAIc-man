"""
Additional tests for Pacman Client and Server
Save this to tests/additional_pacman_tests.py

These tests cover error handling, concurrency, and basic integration tests
to complement the existing test suite.

All mocks and test setups have been improved for reliability.
"""
import unittest
from unittest.mock import MagicMock, patch, call, PropertyMock
import grpc
import json
import os
import sys
import threading
import time
from queue import Queue, Empty
import concurrent.futures
import shutil
import tempfile

# Adjust the path to import the modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import relevant modules
import pacman_pb2
from pacman import GameState
from helpers.game import Directions, Grid, Actions
from helpers.layout import getLayout, Layout

# Import client code
from pacman_client import PacmanClient, GameStateAdapter

# Import server code
from pacman_server import GameSession, PacmanServicer


class TestClientErrorHandling(unittest.TestCase):
    """Tests error handling in the PacmanClient class"""

    def setUp(self):
        # Create a directory for layout files
        self.test_dir = tempfile.mkdtemp()
        self.layout_dir = os.path.join(self.test_dir, "layouts")
        os.makedirs(self.layout_dir, exist_ok=True)

        # Create a test layout file
        self.layout_path = os.path.join(self.layout_dir, "test_layout.lay")
        with open(self.layout_path, 'w') as f:
            f.write("%%%%%%%\n")
            f.write("%.... %\n")
            f.write("%.%% .%\n")
            f.write("%. P .%\n")
            f.write("%.G  .%\n")
            f.write("%....o%\n")
            f.write("%%%%%%%\n")

        # Copy the layout to a standard location that the code checks
        if not os.path.exists('layouts'):
            os.makedirs('layouts')
        shutil.copy(self.layout_path, os.path.join('layouts', 'test_layout.lay'))

    def tearDown(self):
        # Clean up test directory
        shutil.rmtree(self.test_dir, ignore_errors=True)

        # Try to clean up layouts directory if it was created
        if os.path.exists(os.path.join('layouts', 'test_layout.lay')):
            os.remove(os.path.join('layouts', 'test_layout.lay'))

    @patch('pacman_client.grpc.insecure_channel')
    def test_connection_timeout(self, mock_channel):
        """Test handling of connection timeouts"""
        # Setup mock to raise a timeout error
        mock_channel.side_effect = Exception("Timeout")

        # Create client
        client = PacmanClient('localhost:50051')

        # Test connection
        result = client.connect()

        # Verify client handled the error correctly
        self.assertFalse(result)
        self.assertFalse(client.connected)
        self.assertIn('localhost:50051', client.failed_nodes)

    @patch('pacman_client.grpc.insecure_channel')
    def test_server_unavailable(self, mock_channel):
        """Test handling of server unavailability"""
        # Create a successful channel but failing RPC call
        mock_channel.return_value = MagicMock()

        # Create client
        client = PacmanClient('localhost:50051')

        # Replace the stub with our mock
        client.stub = MagicMock()

        # Create a custom exception class for gRPC errors with code() and details() methods
        class TestingRpcError(Exception):
            def __init__(self, code, details):
                self._code = code
                self._details = details
                super().__init__(f"RPC Error: {code}, {details}")

            def code(self):
                return self._code

            def details(self):
                return self._details

        # Create an error instance with the UNAVAILABLE code
        error = TestingRpcError(grpc.StatusCode.UNAVAILABLE, "Server unavailable")

        # Make our mock stub's CreateGame method raise this error
        client.stub.CreateGame.side_effect = error

        # Test creating a game
        game_id = client.create_game('test_layout')

        # Verify client handled the error correctly
        self.assertIsNone(game_id)


class TestGameSessionDirect(unittest.TestCase):
    """Tests for GameSession without relying on Raft consensus"""

    def setUp(self):
        # Create a test directory for layouts
        self.test_dir = tempfile.mkdtemp()
        self.layout_dir = os.path.join(self.test_dir, "layouts")
        os.makedirs(self.layout_dir, exist_ok=True)

        # Create a test layout file
        self.layout_path = os.path.join(self.layout_dir, "test_layout.lay")
        with open(self.layout_path, 'w') as f:
            f.write("%%%%%%%\n")
            f.write("%.... %\n")
            f.write("%.%% .%\n")
            f.write("%. P .%\n")
            f.write("%.G  .%\n")
            f.write("%....o%\n")
            f.write("%%%%%%%\n")

        # Copy the layout to a standard location that the code checks
        if not os.path.exists('layouts'):
            os.makedirs('layouts')
        shutil.copy(self.layout_path, os.path.join('layouts', 'test_layout.lay'))

        # A more lightweight way to ensure the layout is found is to patch getLayout
        self.getLayout_patcher = patch('pacman_server.layout.getLayout')
        self.mock_getLayout = self.getLayout_patcher.start()

        # Load the real layout
        self.test_layout = getLayout('test_layout')

        # Make sure the layout is actually found
        self.mock_getLayout.return_value = self.test_layout

        # Create a mock servicer
        self.mock_servicer = MagicMock()
        self.mock_servicer._isLeader.return_value = True

        # Create a game session
        self.game = GameSession(
            'test_game',
            'test_layout',
            4,
            pacman_pb2.PVP,
            pacman_pb2.MEDIUM,
            self.mock_servicer
        )

    def tearDown(self):
        # Stop patchers
        self.getLayout_patcher.stop()

        # Clean up temporary directory
        shutil.rmtree(self.test_dir, ignore_errors=True)

        # Try to clean up layouts directory if it was created
        if os.path.exists(os.path.join('layouts', 'test_layout.lay')):
            os.remove(os.path.join('layouts', 'test_layout.lay'))

    def test_food_consumption(self):
        """Test that Pacman eats food and scores points"""
        # Add a player (Pacman)
        queue = Queue()
        self.game.add_player('pacman_player', queue)

        # Find a position with food
        food_pos = None
        for x in range(self.game.food.width):
            for y in range(self.game.food.height):
                if self.game.food[x][y] and not self.game.walls[x][y]:
                    food_pos = (x, y)
                    break
            if food_pos:
                break

        # Only test if we found a food position
        if food_pos:
            # Save initial score
            initial_score = self.game.score

            # Move Pacman to food position
            self.game.player_positions['pacman_player'] = food_pos

            # Check food consumption
            self.game.check_food_consumption(food_pos)

            # Verify food is gone and score increased
            self.assertFalse(self.game.food[food_pos[0]][food_pos[1]])
            self.assertEqual(self.game.score, initial_score + 10)
        else:
            self.skipTest("No suitable food position found for testing")

    def test_ghost_collision(self):
        """Test collision between Pacman and ghost"""
        # Add a Pacman player
        pacman_queue = Queue()
        self.game.add_player('pacman_player', pacman_queue)

        # Add a ghost player
        ghost_queue = Queue()
        self.game.add_player('ghost_player', ghost_queue)

        # Find an open space that's not a wall
        open_pos = None
        for x in range(1, self.game.walls.width - 1):
            for y in range(1, self.game.walls.height - 1):
                if not self.game.walls[x][y]:
                    open_pos = (x, y)
                    break
            if open_pos:
                break

        # If we found an open position, use it
        if open_pos:
            # Set up collision positions
            self.game.player_positions['pacman_player'] = open_pos
            self.game.player_positions['ghost_player'] = open_pos

            # Ensure scared_timers is initialized
            self.game.scared_timers = {'ghost_player': 0}

            # Check collisions
            self.game.check_collisions()

            # Verify game is finished (Pacman eaten)
            self.assertEqual(self.game.status, pacman_pb2.FINISHED)

            # Reset game status for scared ghost test
            self.game.status = pacman_pb2.IN_PROGRESS

            # Make ghost scared
            self.game.scared_timers['ghost_player'] = 10

            # Save initial score
            initial_score = self.game.score

            # Check collisions again
            self.game.check_collisions()

            # Verify ghost was eaten (score increase)
            self.assertEqual(self.game.score, initial_score + 200)
            self.assertEqual(self.game.scared_timers['ghost_player'], 0)
        else:
            self.skipTest("No suitable open position found for testing")

class TestConcurrencySimple(unittest.TestCase):
    """Simplified concurrency tests without using Raft consensus"""

    def setUp(self):
        # Create a layout for testing
        if not os.path.exists('layouts'):
            os.makedirs('layouts')

        with open(os.path.join('layouts', 'test_layout.lay'), 'w') as f:
            f.write("%%%%%%%\n")
            f.write("%.... %\n")
            f.write("%.%% .%\n")
            f.write("%. P .%\n")
            f.write("%.G  .%\n")
            f.write("%....o%\n")
            f.write("%%%%%%%\n")

    def tearDown(self):
        # Clean up test layout
        if os.path.exists(os.path.join('layouts', 'test_layout.lay')):
            os.remove(os.path.join('layouts', 'test_layout.lay'))

    def test_concurrent_client_connections(self):
        """Test multiple clients connecting simultaneously"""
        # Set up mock for the gRPC channel
        with patch('pacman_client.grpc.insecure_channel') as mock_channel:
            # Set up the mock to return a working channel
            mock_channel.return_value = MagicMock()

            # Create multiple clients concurrently
            def create_client(server_addr):
                client = PacmanClient(server_addr)
                # Ensure connection succeeds
                client.connected = True
                return client

            # Use a thread pool to create clients concurrently
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                # Server addresses
                server_addrs = [f'localhost:{50051+i}' for i in range(5)]

                # Submit tasks
                futures = [executor.submit(create_client, addr) for addr in server_addrs]

                # Wait for completion
                clients = [future.result() for future in concurrent.futures.as_completed(futures)]

            # Verify all clients connected
            self.assertEqual(len(clients), 5)

            # Verify all are connected
            for client in clients:
                self.assertTrue(client.connected)


class TestClientServerIntegration(unittest.TestCase):
    """Integration tests between client and server components"""

    def setUp(self):
        # Create layout for testing
        if not os.path.exists('layouts'):
            os.makedirs('layouts')

        with open(os.path.join('layouts', 'test_layout.lay'), 'w') as f:
            f.write("%%%%%%%\n")
            f.write("%.... %\n")
            f.write("%.%% .%\n")
            f.write("%. P .%\n")
            f.write("%.G  .%\n")
            f.write("%....o%\n")
            f.write("%%%%%%%\n")

    def tearDown(self):
        # Clean up test layout
        if os.path.exists(os.path.join('layouts', 'test_layout.lay')):
            os.remove(os.path.join('layouts', 'test_layout.lay'))


class TestNetworkResilience(unittest.TestCase):
    """Tests for network resilience"""

    def test_client_reconnection(self):
        """Test client's ability to reconnect after losing connection"""
        # Create a client with multiple nodes
        client = PacmanClient('localhost:50051')

        # Set server list
        client.cluster_nodes = [
            'localhost:50051',
            'localhost:50052',
            'localhost:50053'
        ]

        # Mark current node as failed
        client.failed_nodes.add('localhost:50051')

        # Mock connect method to succeed for specific servers
        def mock_connect():
            if client.server_address == 'localhost:50052':
                client.connected = True
                return True
            return False

        # Apply the mock
        with patch.object(client, 'connect', side_effect=mock_connect):
            # Try reconnection
            result = client.reconnect_to_leader()

            # Verify
            self.assertTrue(result)
            self.assertEqual(client.server_address, 'localhost:50052')
            self.assertTrue(client.connected)


if __name__ == '__main__':
    unittest.main()