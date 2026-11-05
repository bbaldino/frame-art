"""The one-time upgrade to 0.12.0: fill the Library tables from the old ones.

Nothing old is deleted or rewritten, so an older release can still start
against the database. The new rows are written with ``insert or ignore`` in one
transaction and the ``library_0_12`` flag is set last, so an interrupted
upgrade can be run again without touching anything decided since.
"""

from __future__ import annotations

import logging
import time

from . import library
from .catalogue import Catalogue
from .studio import StudioStore

log = logging.getLogger(__name__)

FLAG = "library_0_12"


def upgrade(cat: Catalogue, studio: StudioStore) -> bool:
    """Move the collection, bans, portraits and versions into the Library, once.

    Returns whether it ran. The first nightly run after it must not churn the
    TV: everything live is in the Library (and not Removed), including a
    version file that is live now though it is no longer the current one.
    """
    if cat.get(FLAG):
        return False
    db = cat.db
    now = time.time()
    with db:
        # 1. When each work first entered Works: the earliest sign of it.
        db.execute(
            "insert or ignore into work_added "
            "select w.key, min(w.crawled_at, "
            "  coalesce((select rendered_at from rendered where key = w.key), w.crawled_at), "
            "  coalesce((select rejected_at from rejected where key = w.key), w.crawled_at), "
            "  coalesce((select min(published_at) from published where key = w.key), "
            "           w.crawled_at)) "
            "from works w"
        )
        # 2. When each source was last crawled.
        db.execute(
            "insert or ignore into source_crawls "
            "select source, max(crawled_at) from works group by source"
        )
        # 3. The Library: the collection, everything ever on the TV, the portraits.
        first = dict(
            db.execute("select key, min(published_at) from published group by key")
        )
        saved = {f"{library.PORTRAIT}{p.id}": p.saved_at for p in studio.paintings()}
        collected = cat.collection()
        banned = {k for k, s in cat.overrides().items() if s == "banned"}
        rows = []
        for key in dict.fromkeys([*collected, *first, *saved]):
            if key in banned:
                continue
            at = collected.get(key) or first.get(key) or saved.get(key) or now
            rows.append((key, "in", at))
        # 4. Bans become Removed.
        rows += [
            (k, "removed", at)
            for k, at in db.execute("select key, set_at from overrides")
            if k in banned
        ]
        # 5. Versions: only the current one, unless vetoed, stays eligible. The
        # one live on the TV right now stays too, so the first run keeps it.
        files = set(cat.published_variants())
        on_tv = {
            v
            for f, v in db.execute(
                "select pv.filename, pv.variant from published_variants pv "
                "join published p using (filename) where p.retired_at is null"
            )
            if f in files
        }
        current = {
            (key, type): None if dont_use else cur
            for key, type, cur, dont_use in db.execute(
                "select key, type, current, dont_use from variant_state"
            )
        }
        for vid, key, type in db.execute("select id, key, type from variants"):
            if current.get((key, type)) == vid or vid in on_tv:
                continue
            rows.append((f"{library.VARIANT}{vid}", "removed", now))
        db.executemany("insert or ignore into library values (?, ?, ?)", rows)
        # Last: if anything above failed, the next start runs it all again.
        cat.db.execute("insert or replace into kv values (?, 'true')", (FLAG,))
    log.info("upgraded to the Library: %d works placed", len(rows))
    return True
