"""A real recorded day, loadable with no API key.

The whole application is behind a paid run. A reviewer who clones this reads a
README and a `docker compose up` that produces an empty page with a button they
cannot press, because pressing it needs a key they have no reason to buy — so
the four screens the project is actually about are invisible to everyone except
the person who built it.

`ainews demo export` writes one or more real bulletins, with the runs behind
them and the reader's own labels, into `demo.json` inside this package.
`ainews demo seed` loads that file into an empty database, rebasing every
timestamp so the newest bulletin is today. The page then reads as it does on a
machine that has been running for a week.

**No article bodies.** The export carries headlines, links and the model's own
writing; it does not carry the articles. The repository is public and an article
is someone else's text — the same rule that keeps a body out of the evaluation
fixtures (ADR 0019 §3). The cost is stated where it lands: `ainews eval judge`
has nothing to read on a seeded database, and says so.

**It says on screen what it is.** `DEMO_MODE=true` draws a band above the
bulletin naming the day the run really happened. Seeded data pretending to be
this morning's news would be the one dishonest screen in the project.
"""

from __future__ import annotations

from ainews.demo.seed import (
    DEMO_PATH,
    DemoData,
    export_demo,
    load_demo,
    seed_demo,
    write_demo,
)

__all__ = [
    "DEMO_PATH",
    "DemoData",
    "export_demo",
    "load_demo",
    "seed_demo",
    "write_demo",
]
