"""
SUNI benchmark subsystem.

Two separate data sources, deliberately kept apart (see metrics.py):

  * Passive telemetry  — harvested from *real* inference as SUNI is used.
                         Always-on, cheap, reflects the live workload.
                         See telemetry.py.
  * On-demand suites   — a fixed eval battery run against the configured
                         model on admin request. Reflects *capability*,
                         not usage, and makes SUNI slow while it runs.
                         See runner.py + suites/.

Every one of the 33 metrics from the reference article carries a status
badge (live / on-demand / estimated / n-a) so the dashboard never renders a
fabricated number. Metrics with no trustworthy local measurement are shown
as N/A with a stated reason rather than guessed.
"""
from .metrics import METRICS, by_category, metric  # noqa: F401
from . import telemetry  # noqa: F401
