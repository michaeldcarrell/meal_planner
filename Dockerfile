FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Claude calls (plan generation, replacements) can take a minute or two.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn meal_planner.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 300"]
