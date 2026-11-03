# Guide sources

`docs/ghmirror-guide.pdf` is built from these files.

| File | Role |
|---|---|
| `part1.html` … `partN.html` | The chapters, concatenated in number order (part1 holds the CSS, cover and table of contents). |
| `imgj/*.jpg` | Screenshots as used in the PDF (1800 px wide). |
| `build.py` | Concatenates the parts, renders twice to fill the table-of-contents page numbers, swaps in a footer-less cover, writes `ghmirror-guide.pdf`. |
| `render.js` | Renders HTML to A4 PDF with Chrome (playwright-core) and the page footer. |
| `shoot.js`, `crop.py` | Re-take screenshots from a live (disposable) GitLab: `GITLAB_ROOT_PASSWORD=... node shoot.js http://localhost:8931 pages.json`, then `python crop.py` writes `img/*.png`, which `build.py` converts into `imgj/`. |

```sh
cd docs/guide-src
python3 -m venv .venv && .venv/bin/pip install pillow pypdf pypdfium2
npm install                       # playwright-core 1.48.2; uses the installed Google Chrome (CHROME=... to override)
.venv/bin/python build.py && cp ghmirror-guide.pdf ..
```
