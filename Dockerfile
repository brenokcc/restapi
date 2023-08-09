FROM python:alpine
RUN apk add zlib-dev jpeg-dev build-base git libffi-dev
RUN pip install -q --no-cache-dir djangorestframework markdown django-filter drf-yasg
WORKDIR /opt/pnp
ADD . /opt/pnp
EXPOSE 8888
RUN pip install -q --no-cache-dir coreapi

