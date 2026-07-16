# Python 3.12 — стабильно поддерживается aiogram (3.14 брать не стоит: свежий, aiogram может не поддерживать)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# data/ и media/ монтируются как volume, чтобы состояние и картинки переживали рестарт.
# bot.app при первом старте сам импортирует контент из xlsx, если variants != 66.
CMD ["python", "-m", "bot.app"]
