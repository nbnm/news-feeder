import os

# Importing app.main creates the ASGI app. Keep that implicit app isolated too,
# before test-module collection, so upgrade hooks never touch a running archive.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["WORKER_ENABLED"] = "false"
