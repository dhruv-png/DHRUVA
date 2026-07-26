"""Process configuration.

The **only** module in the codebase permitted to read the environment. Boundary
rule R5 fails the build if any other module imports ``os.environ``, ``os.getenv``
or ``dotenv`` (ADR-031).

Contains at S02:

* :class:`~dhruva.shared.config.environment.Environment` -- the closed set of
  deployment environments;
* :class:`~dhruva.shared.config.secret.SecretValue` -- a credential that never
  renders itself (ADR-033).

The ``Settings`` schema and its loader land in the next commit of this subsystem.
"""

from __future__ import annotations

from dhruva.shared.config.environment import Environment
from dhruva.shared.config.secret import REDACTED_PLACEHOLDER, SecretValue, registered_secret_values

__all__ = ["REDACTED_PLACEHOLDER", "Environment", "SecretValue", "registered_secret_values"]
