#!/bin/bash
# Start script for the Stra2us CLI client

if [ ! -d "venv" ]; then
    echo "Virtual environment not found! Please run 'python3 -m venv venv && source venv/bin/activate && pip install -r client-requirements.txt' first."
    exit 1
fi

source venv/bin/activate
python3 test_client.py $*
# uvicorn src.main:app --host $HOST --port $PORT --reload
