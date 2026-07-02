#!/bin/bash
cd /home/ubuntu/apps/SigenStor
set -a
source .env
set +a
/home/ubuntu/apps/SigenStor/venv/bin/python scripts/calculate_savings.py --auto
