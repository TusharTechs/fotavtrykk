"""External-footprint connectors.

Each one resolves to the exact legal entity before publishing anything. A
connector that can only match on name does not ship — that tier was measured at
84% precision and dropped.
"""

from .nav_jobs import NavJobsSource
from .places import PlacesSource

__all__ = ["NavJobsSource", "PlacesSource"]
