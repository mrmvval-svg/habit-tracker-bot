FROM python:3.11-slim

WORKDIR /app

# чтобы не было лишнего мусора и буферизации логов
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код
COPY . .

CMD ["python", "bot.py"]
