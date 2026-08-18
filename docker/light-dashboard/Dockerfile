FROM python:3.11-slim

WORKDIR /app

# Copy backend dependencies and install
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake DuckDB's sqlite extension into the image. It is downloaded on first use
# otherwise, which would put a network fetch on the path of the first metrics
# request and break the tab entirely on a host without egress. INSTALL is a
# no-op at runtime once the extension is already present.
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL sqlite')"

# The test runner, in its own layer after the runtime deps so editing it never
# invalidates the install above. In the image rather than outside it because
# compose bind-mounts the source over this container and runs it with --reload:
# this *is* where the code runs, so it is where the tests should run.
#
#   docker compose exec hermes-light-dashboard pytest
#
COPY backend/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# Copy backend application files and the single-page React frontend
COPY backend /app/backend
COPY frontend /app/frontend
# Rootdir marker for pytest — without it, `pytest` inside the container finds
# no configuration and collects from whatever the working directory happens to
# be. See the file for why it must sit beside backend/ rather than inside it.
COPY pytest.ini /app/pytest.ini

# Expose the new custom lightweight dashboard port
EXPOSE 9120

# Command to run the FastAPI server
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "9120"]
