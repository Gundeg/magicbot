web: WORKER_ROLE=web gunicorn app:app --workers 2 --timeout 60 --bind 0.0.0.0:$PORT
worker: WORKER_ROLE=worker python -u worker.py
