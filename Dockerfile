# Container voor de always-on PocketPop-tracker (VPS / Fly.io / Railway).
FROM python:3.12-slim

WORKDIR /app
COPY tracker/requirements.txt tracker/requirements.txt
RUN pip install --no-cache-dir -r tracker/requirements.txt

COPY tracker/ tracker/
ENV INTERVAL=60

# Draait de loop; env-variabelen (SUPABASE_*, BOL_*, DISCORD_WEBHOOK) geef je
# mee via de host (docker run --env-file .env, of Fly/Railway secrets).
CMD ["bash", "tracker/run_loop.sh"]
