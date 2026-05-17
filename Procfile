web: uvicorn openbot.webapp:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-access-log
worker: python -m openbot.queue.runner
