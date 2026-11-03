"""Assemble guide.html from the parts, fill the TOC page numbers (two passes), render, put a footer-less cover."""
import glob, json, os, re, subprocess, sys
from PIL import Image
from pypdf import PdfReader, PdfWriter

os.makedirs("imgj", exist_ok=True)
for f in glob.glob("img/*.png"):
    out = "imgj/" + os.path.basename(f)[:-4] + ".jpg"
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(f):
        im = Image.open(f).convert("RGB")
        im.resize((1800, round(im.height * 1800 / im.width)), Image.LANCZOS).save(out, quality=86, optimize=True)

parts = sorted(glob.glob("part*.html"), key=lambda f: int(re.search(r"\d+", f).group()))
html = "".join(open(f).read() for f in parts) + "\n</body></html>\n"
html = re.sub(r'src="img/([^"]+)\.png"', r'src="imgj/\1.jpg"', html)

def render(h, out, footer):
    open("guide.html", "w").write(h)
    p = subprocess.run(["node", "render.js", "guide.html", out, "1" if footer else "0"], capture_output=True, text=True, check=True)
    return json.loads(p.stdout.strip().splitlines()[-1])

import unicodedata
norm = lambda s: re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or ""))

def find_pages(pdf, heads):
    texts = [norm(p.extract_text()) for p in PdfReader(pdf).pages]
    start = next(i for i, t in enumerate(texts) if "1Executivesummary" in t and i > 1)
    pages, cur = {}, start
    for sid, title in heads:
        key = norm(title)
        for i in range(cur, len(texts)):
            if key in texts[i]:
                pages[sid] = i + 1; cur = i; break
        else:
            sys.exit(f"heading not found: {sid} {title!r}")
    return pages

def fill(h, pages):
    return re.sub(r'<span class="pg" data-for="([^"]+)">[^<]*</span>',
                  lambda m: f'<span class="pg" data-for="{m.group(1)}">{pages[m.group(1)]}</span>', h)

heads = render(html, "pass1.pdf", True)
pages = find_pages("pass1.pdf", heads)
for _ in range(3):
    h2 = fill(html, pages)
    render(h2, "pass2.pdf", True)
    again = find_pages("pass2.pdf", heads)
    if again == pages:
        break
    pages = again
render(h2, "nofooter.pdf", False)
w = PdfWriter()
body, bare = PdfReader("pass2.pdf"), PdfReader("nofooter.pdf")
assert len(body.pages) == len(bare.pages)
w.add_page(bare.pages[0])
for p in body.pages[1:]:
    w.add_page(p)
w.add_metadata({"/Title": "ghmirror — Architecture, Setup and Operations Guide",
                "/Subject": "One-to-one GitHub organization mirror into self-managed GitLab (cloud + offline)"})
w.compress_identical_objects()
with open("ghmirror-guide.pdf", "wb") as f:
    w.write(f)
print(len(body.pages), "pages;", json.dumps(pages), f"{os.path.getsize('ghmirror-guide.pdf')/1e6:.1f} MB")
