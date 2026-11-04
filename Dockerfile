# Turns Pipfile.lock into a pip requirements file, keeping pipenv out of the final image.
FROM python:3.13-slim-trixie AS lock
WORKDIR /lock
RUN pip install --no-cache-dir pipenv==2024.4.1
COPY Pipfile Pipfile.lock ./
RUN pipenv requirements --hash > requirements.txt


FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# The lock is made on Windows, so it lists no NVIDIA libraries and PyPI's Linux torch wheel
# could not load. The CPU wheel of the same version needs none of them and is far smaller.
COPY --from=lock /lock/requirements.txt /tmp/requirements.txt
RUN TORCH_VERSION="$(sed -n 's/^torch==\([^ ;]*\).*/\1/p' /tmp/requirements.txt)" \
    && test -n "$TORCH_VERSION" \
    && pip install --no-deps --index-url https://download.pytorch.org/whl/cpu "torch==$TORCH_VERSION" \
    && sed -i '/^torch==/d' /tmp/requirements.txt \
    && pip install --no-deps --require-hashes -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt

# Headless browser for pages that are empty until JavaScript runs.
RUN playwright install --with-deps chromium

RUN useradd --create-home --uid 1000 app

COPY . .
RUN python manage.py collectstatic --noinput

USER app
EXPOSE 8000

# Manual crawls run in a background thread of the web worker, so workers must not be recycled
# with --max-requests: that would interrupt a crawl. Progress is kept in the database, so any
# worker can serve the dashboard.
CMD ["gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-class", "gthread", "--threads", "8", "--timeout", "120", "--access-logfile", "-"]
