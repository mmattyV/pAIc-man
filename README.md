# pAIc-man
Multiplayer pac-man built on a distributed system with AI agents playing the ghosts or pac-man.

## Prerequisites

- Python 3.10+
- Git

## Initial Setup

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd pAIc-man
   ```

2. Set up the environment (choose one method):

   **Using Conda:**
   ```bash
   conda env create -f environment.yml
   conda activate paic-man
   ```

   **Using pip:**
   ```bash
   pip install -r requirements.txt  # If requirements.txt exists
   ```

3. Install gRPC dependencies for multiplayer:
   ```bash
   pip install grpcio grpcio-tools
   ```

4. Create necessary directories:
   ```bash
   mkdir -p logs data
   ```

5. Generate gRPC code from protocol buffer definition:
   ```bash
   python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. pacman.proto
   ```

## Running the Single-Player Game

To play the original single-player Pac-Man game:
```bash
python pacman.py
```

## Running the Multiplayer Game

### 1. Start the Server

Start the server first before running any clients:

```bash
python pacman_server.py
```

Server options:
- `--port 50051` - Specify port (default: 50051)
- `--data-dir ./data` - Specify data directory (default: ./data)

Example with custom port:
```bash
python pacman_server.py --port 50052
```

### 2. Run the Client

After the server is running, start one or more clients:

```bash
python pacman_client.py
```

Client options:
- To connect to a local server on default port: (no arguments needed)
- To connect to a remote server: `--server hostname:50051`

Example connecting to remote server:
```bash
python pacman_client.py --server 192.168.1.100:50051
```

### Playing the Game

1. When you start the client, you'll see a menu with options to:
   - List available games
   - Create a new game
   - Join an existing game
   - Exit

2. To create a game:
   - Select "Create a new game"
   - Enter a layout name (default: "mediumClassic")
   - Enter max players (default: 4)
   - Note the game ID that is generated

3. To join a game:
   - Select "Join a game"
   - Enter the game ID
   - Use arrow keys to move and 'q' to quit

## Multiplayer Architecture

The project uses a client-server architecture with gRPC for communication:

- **pacman.proto**: Defines the protocol buffer messages and services
- **pacman_server.py**: Implements the game server that manages game sessions and state
- **pacman_client.py**: Provides the client interface to connect, join games, and send actions

## Game Features

- Multiple players can connect to a single server
- First player becomes Pac-Man, others play as ghosts
- Server manages the central game state
- Clients send player actions and receive game state updates
- Basic fault tolerance with server reconnection capability

## Troubleshooting

- If you encounter connection errors, ensure the server is running and check the specified address
- Check logs in the `logs` directory (server.log and client.log) for detailed error information
- If you receive protobuf-related errors, regenerate the gRPC code using the command in step 5 above

- Update the environment with `conda env update --file environment.yml --prune`

