#!/bin/bash

cd ~/Desktop/Robert || exit 1

source .venv/bin/activate

set -a
source .env
set +a

export FLASK_APP=dcmsts

flask run --host=127.0.0.1 --port=5000
