import collections
import json

from clawbox.lab import sdk


rows = sdk()[0].list_v2()
print(json.dumps({
    "count": len(rows),
    "states": dict(collections.Counter(str(row.get("state")) for row in rows)),
    "running": [
        {
            "id": row.get("id") or row.get("sandboxID") or row.get("sandbox_id"),
            "metadata": row.get("metadata"),
        }
        for row in rows if row.get("state") == "running"
    ],
}, indent=2))
