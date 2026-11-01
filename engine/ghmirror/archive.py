"""L2 raw archive: every GitHub object as JSON, previous versions kept, binary assets alongside."""
import json
import os
import re
import time

from .mapping import digest


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.@+-]", "_", str(s))


class Archive:
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _write(self, path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = json.dumps(obj, indent=1, sort_keys=True, default=str)
        if os.path.exists(path):
            with open(path) as f:
                old = f.read()
            if old == data:
                return False
            hist = os.path.join(os.path.dirname(path), "_history", os.path.basename(path)[:-5])
            os.makedirs(hist, exist_ok=True)
            with open(os.path.join(hist, f"{time.strftime('%Y%m%dT%H%M%S')}-{digest(old)}.json"), "w") as f:
                f.write(old)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, path)
        return True

    def put(self, repo_full, kind, key, obj):
        return self._write(os.path.join(self.root, *repo_full.split("/"), kind, _safe(key) + ".json"), obj)

    def put_global(self, kind, key, obj):
        return self._write(os.path.join(self.root, "_global", kind, _safe(key) + ".json"), obj)

    def get(self, repo_full, kind, key):
        p = os.path.join(self.root, *repo_full.split("/"), kind, _safe(key) + ".json")
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def list(self, repo_full, kind):
        d = os.path.join(self.root, *repo_full.split("/"), kind)
        if not os.path.isdir(d):
            return {}
        out = {}
        for name in os.listdir(d):
            if name.endswith(".json"):
                with open(os.path.join(d, name)) as f:
                    out[name[:-5]] = json.load(f)
        return out

    def blob_path(self, repo_full, kind, key, filename):
        d = os.path.join(self.root, *repo_full.split("/"), kind, _safe(key))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, _safe(filename))

    def migrations_dir(self):
        d = os.path.join(self.root, "_migrations")
        os.makedirs(d, exist_ok=True)
        return d
