#!/bin/bash
# Start script for the Stra2us IoT Backend

# Default settings
HOST="127.0.0.1"
PORT=8000

# Simple argument parsing
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --host) HOST="$2"; shift ;;
        --port) PORT="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

echo "Checking dependencies..."

# Check Redis
if ! command -v redis-cli &> /dev/null; then
    echo "ERROR: 'redis-cli' not found. Please install Redis (sudo apt install redis-server)."
    exit 1
fi

if ! redis-cli ping &> /dev/null; then
    echo "ERROR: Redis is not running! Please start it with 'sudo service redis-server start' or 'redis-server'."
    exit 1
fi

echo "Redis is online. Starting Stra2us IoT Backend on $HOST:$PORT..."

if [ ! -d "venv" ]; then
    echo "Virtual environment not found! Please run 'python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt' first."
    exit 1
fi

source venv/bin/activate
uvicorn src.main:app --host $HOST --port $PORT --reload
