#!/bin/bash
cd /home/ubuntu/apps/SigenStor
set -a
source .env
set +a
/home/ubuntu/apps/SigenStor/venv/bin/python scripts/mail_fetcher.py
/home/ubuntu/apps/SigenStor/venv/bin/python scripts/process_zip.py
