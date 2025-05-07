#!/bin/bash
# start_cluster.sh - Script to start multiple server instances for pAIcMan

# Kill any existing server processes
echo "Stopping any existing server processes..."
pkill -f "python pacman_server.py" || true

# Clean up old data for a fresh start
echo "Cleaning up old data..."
rm -rf ./data
mkdir -p logs
mkdir -p data

# Define Raft addresses (separate from gRPC ports)
raft_addrs=(
  "127.0.0.1:4321"
  "127.0.0.1:4322"
  "127.0.0.1:4323"
  "127.0.0.1:4324"
  "127.0.0.1:4325"
)

# Define gRPC ports
grpc_ports=(
  "50051"
  "50052"
  "50053"
  "50054"
  "50055"
)

# Start 5 server replicas (for 2-fault tolerance)
echo "Starting 5-node Raft cluster..."
for i in {0..4}; do
  port=${grpc_ports[$i]}
  self_addr=${raft_addrs[$i]}
  
  # Create partner_addrs array (all addresses except self)
  partner_addrs=""
  for j in {0..4}; do
    if [ $j -ne $i ]; then
      partner_addrs="$partner_addrs ${raft_addrs[$j]}"
    fi
  done
  
  data_dir="./data/node_$port"
  mkdir -p "$data_dir"
  
  echo "Starting server on gRPC port $port with Raft address $self_addr"
  echo "  Partner nodes: $partner_addrs"
  
  python pacman_server.py --port $port --data-dir "$data_dir" \
    --self-addr "$self_addr" --partner-addrs $partner_addrs &

  # Add a small delay to avoid port conflicts
  sleep 1
done

echo "Raft cluster is running. Use 'pkill -f \"python pacman_server.py\"' to stop all servers."
echo "Check logs/server.log for server output."
echo ""
echo "Usage instructions:"
echo "  - Connect to any node (ports 50051, 50052, 50053, 50054, or 50055)"
echo "  - If the leader node fails, the client should reconnect to another node"
echo "  - Game state is automatically replicated across all nodes"
echo "  - With 5 nodes, the system can tolerate up to 2 simultaneous failures"
