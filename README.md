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

#### Running a Fault-Tolerant Server Cluster

To run a 5-node Raft cluster for fault tolerance:

```bash
./start_cluster.sh
```

This will start three server instances on ports 50051, 50052, and 50053 with shared state replication.

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

### 3. Testing Fault Tolerance

One of the key features of pAIc-man is its fault tolerance through Raft consensus. You can test this by deliberately killing one of the server instances while playing:

```
# Find the process ID of the server running on a specific port (e.g., 50052)
lsof -i :50052 | grep LISTEN | awk '{print $2}'

# Kill the server process using the process ID
kill <process_id>
```

Alternatively, if you're running the servers in separate terminal windows, you can simply press `Ctrl+C` in the terminal of the server you want to kill.

The system is designed to maintain game state and continue operation as long as a majority of servers (3 out of 5 in a standard setup) remain functional. After killing a server:

1. If you killed a follower server, gameplay should continue uninterrupted
2. If you killed the leader server, there might be a brief pause while a new leader is elected, but the system should recover automatically
3. You can observe server logs to see the election process and state replication in action


### Playing the Game

1. When you start the client, you'll see a menu with options to:
   - List available games
   - Create a new game
   - Join an existing game
   - Exit

2. To create a game:
   - Select "Create a new game"
   - Enter a layout name (default: "mediumClassic")
   - Select gamemode (PVP or AI Pacman)
   - Note the game ID that is generated

3. To join a game:
   - Select game with desired ID
   - Select "Join a game"

4. Game Controls:
   - Movement: Arrow keys or WASD keys
   - Return to menu: ESC key
   - Quit game: Q key

### AI Pacman Mode

pAIc-man features an AI-controlled Pacman mode where human players play as ghosts against a computer-controlled Pacman:

1. When creating a game:
   - Select "AI Pacman" from the game mode radio buttons
   - Choose an AI difficulty level (Easy, Medium, or Hard)
   - The more difficult the AI, the smarter Pacman will be at collecting pellets and avoiding ghosts

2. Gameplay differences:
   - All human players are automatically assigned to ghost roles
   - Pacman is controlled by an AI algorithm with different strategies based on difficulty
   - Easy: Makes random moves but avoids walls
   - Medium: Seeks food while maintaining a safe distance from ghosts
   - Hard: Uses sophisticated pathfinding, prioritizes power pellets when ghosts are nearby, and strategically chases scared ghosts

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

## Fault Tolerance

The server includes the following fault tolerance features:

1. **State Replication**: Game state is replicated across multiple server nodes using the Raft consensus algorithm (via PySyncObj)
2. **Leader Election**: If the leader node fails, a new leader is automatically elected
3. **Command Replication**: Game commands are replicated to ensure consistency across server nodes
4. **Reconnection Handling**: Clients can reconnect to any server in the cluster if their connection is lost
5. **State Versioning**: A monotonic versioning system prevents applying outdated state updates

## Troubleshooting

- If you encounter connection errors, ensure the server is running and check the specified address
- Check logs in the `logs` directory (server.log and client.log) for detailed error information
- If you receive protobuf-related errors, regenerate the gRPC code using the command in step 5 above
- For rubber-banding issues, ensure you're using the latest version of grpcio (1.71.0+)

- Update the environment with `conda env update --file environment.yml --prune`
