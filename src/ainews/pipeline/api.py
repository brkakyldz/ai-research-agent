"""What the web layer is allowed to ask the pipeline.

One import surface, so a route or a query never reaches past it into a node.
Before this module the web layer reached in three times and did it from inside
function bodies, because the top of the file was where the cycle showed:
`queries.count_candidates` imported `nodes.dedupe`, `routes/sources` imported
`nodes.collect`, `views.build_advice` imported `runner`. A function-level import
is what a cycle looks like once it has been worked around rather than removed.

The direction is one way and stays one way: `pipeline` never imports `web`. That
is the whole reason this file is on this side of the boundary — the pipeline
decides what it is willing to answer, and the answer is three questions and one
repair, none of which starts a run.
"""

from __future__ import annotations

from ainews.pipeline.nodes.collect import probe_feed
from ainews.pipeline.nodes.dedupe import count_candidates
from ainews.pipeline.runner import RunBusy, digest_in_flight, reconcile_orphaned_runs

__all__ = [
    # What a refused run raises.
    "RunBusy",
    # How many articles a press would summarise, before it is pressed.
    "count_candidates",
    # Whether a paid run is in flight - what makes the button dead.
    "digest_in_flight",
    # Does this URL parse as a feed, and what is in it. Read-only, no writes.
    "probe_feed",
    # The one entry here that writes: closing run rows a killed process left
    # open. Startup repair rather than work - it finishes runs and cannot start
    # one.
    "reconcile_orphaned_runs",
]
