"""`python -m target_app` serves both tenants on http://127.0.0.1:4599."""

import uvicorn

uvicorn.run("target_app.app:app", host="127.0.0.1", port=4599, log_level="warning")
