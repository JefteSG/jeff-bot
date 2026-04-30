FROM python:3.12-slim

RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./

# selfcord.py==1.0.3 precisa de --no-deps para evitar aiohttp==3.8.5, incompatível com Python 3.12
RUN pip install --upgrade pip && \
    pip install --no-deps selfcord.py==1.0.3 && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install discord.py==2.1.0

COPY . .

EXPOSE 8000

ENV APP_ENV=production
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/bot:/app

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
