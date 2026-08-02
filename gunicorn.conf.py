import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
# SQLite serializes writes at the file level, so more worker *processes* just means more
# contention/"database is locked" errors. One worker with several threads gives real
# concurrency for reads (and for the I/O-bound request handling generally) while keeping
# writes sane. Bump GUNICORN_WORKERS only if you migrate off SQLite to Postgres/MySQL.
worker_class = "gthread"
workers = int(os.environ.get('GUNICORN_WORKERS', 1))
threads = int(os.environ.get('GUNICORN_THREADS', 4))
timeout = int(os.environ.get('GUNICORN_TIMEOUT', 60))
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info')
