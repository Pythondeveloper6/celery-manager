# Load the core models before any other table module. The satellite modules
# (health_data, auth_state, ...) import `Base` from models, and models imports
# them back at its end so `Base.metadata` is complete; entering through a
# satellite first would otherwise hit that cycle half-initialized.
from celery_manager.db import models as _models  # noqa: F401
