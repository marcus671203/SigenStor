#!/bin/bash
# Wrapper script that loads .env and runs the poller
cd /home/ubuntu/apps/SigenStor
set -a
source .env
set +a
exec /home/ubuntu/apps/SigenStor/venv/bin/python scripts/poll_db.py
