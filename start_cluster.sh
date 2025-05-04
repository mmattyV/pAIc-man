#!/bin/bash
# start_cluster.sh - Script to start multiple server instances for pAIcMan

# Create necessary directories
mkdir -p logs
mkdir -p data

# Kill any existing server processes
echo "Stopping any existing server processes..."
pkill -f "python pacman_server.py" || true

# Start 5 server replicas
echo "Starting 5 server replicas..."
for port in {50051..50055}; do
  data_dir="./data/raft_$port"
  mkdir -p "$data_dir"
  echo "Starting server on port $port with data directory $data_dir"
  python pacman_server.py --port $port --data-dir "$data_dir" &

  # Add a small delay to avoid port conflicts
  sleep 0.5
done

echo "Server cluster is running. Use 'pkill -f \"python pacman_server.py\"' to stop all servers."
echo "Check logs/server.log for server output."
