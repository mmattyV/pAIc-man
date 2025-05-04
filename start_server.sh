#!/bin/bash
# start_server.sh - Script to start a single server instance for pAIcMan

# Create necessary directories
mkdir -p logs
mkdir -p data

# Kill any existing server processes
echo "Stopping any existing server processes..."
pkill -f "python pacman_server.py" || true

# Start single server on port 50051
echo "Starting server on port 50051..."
data_dir="./data/server"
mkdir -p "$data_dir"
python pacman_server.py --port 50051 --data-dir "$data_dir"

echo "Server is running on port 50051."
echo "Check logs/server.log for server output."
