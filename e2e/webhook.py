"""Send a GitHub-style signed webhook delivery to the engine (GitHub cannot reach localhost)."""
import hashlib
import hmac
import json
import sys
import uuid

import requests

url, secret, event, repo = sys.argv[1:5]
extra = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
delivery = extra.pop("_delivery", None) or str(uuid.uuid4())
bad = extra.pop("_bad_signature", False)
body = json.dumps(dict({"repository": {"full_name": repo}}, **extra)).encode()
sig = "sha256=" + hmac.new((secret + ("x" if bad else "")).encode(), body, hashlib.sha256).hexdigest()
r = requests.post(url, data=body, headers={"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                                           "X-Hub-Signature-256": sig, "Content-Type": "application/json"})
print(r.status_code, r.text)
