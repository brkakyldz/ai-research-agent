"""What the web layer is allowed to ask the pipeline.

One import surface, so a route or a query never reaches past it into a node.
Before this module the web layer reached in three times and did it from inside
function bodies, because the top of the file was where the cycle showed:
`queries.count_candidates` imported `nodes.dedupe`, `routes/sources` imported
`nodes.collect`, `views.build_advice` imported `runner`. A function-level import
is what a cycle looks like once it has been worked around rather than removed.

The direction is one way and stays one way: `pipeline` never imports `web`. That
is the whole reason this file is on this side of the boundary — the pipeline
decides what it is willing to answer, and the answer is three questions, none of
which starts work.
"""

from __future__ import annotations

from ainews.pipeline.nodes.collect import probe_feed
from ainews.pipeline.nodes.dedupe import count_candidates
from ainews.pipeline.runner import Resumable, RunBusy, digest_in_flight, resumable_run

__all__ = [
    # The failed run a press could finish instead, and its shape.
    "Resumable",
    # What a refused run raises.
    "RunBusy",
    # How many articles a press would summarise, before it is pressed.
    "count_candidates",
    # Whether a paid run is in flight - what makes the button dead.
    "digest_in_flight",
    # Does this URL parse as a feed, and what is in it. Read-only, no writes.
    "probe_feed",
    "resumable_run",
]
