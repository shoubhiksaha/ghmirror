# Sample configuration

| File | Goes to | What it is |
|---|---|---|
| [`ghmirror.yaml`](ghmirror.yaml) | `deploy/compose/ghmirror.yaml` | The engine configuration exactly as `ghmirror init` writes it. Every key is explained in [`deploy/compose/ghmirror.example.yaml`](../deploy/compose/ghmirror.example.yaml). |
| [`env.example`](env.example) | `deploy/compose/.env` | Secrets, one per line, read by Docker Compose. |
| [`identities.example.yaml`](identities.example.yaml) | `deploy/compose/identities.yaml` | GitHub login to company email. |

You normally do not write these by hand: `deploy/compose/setup.sh` generates all three (see
[`docs/INSTALL.md`](../docs/INSTALL.md)). They are here to review before installing, to diff against a
running install, and for configuration management tools that template the files themselves.
