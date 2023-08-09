docker build -t pnp-api .
docker run --rm -p 8000:8000 pnp-api python manage.py runserver 0.0.0.0:8000