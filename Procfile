web: uvicorn openbot.entrypoints.api.app:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*' --no-access-log
worker: python -m openbot.entrypoints.worker
