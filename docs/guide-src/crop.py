from PIL import Image
import sys
L = 480  # left edge of GitLab's content area (sidebar removed)
crops = {
 '01-group': [(L, 95, 2880, 520)],
 '02-project': [(L, 95, 2880, 1420)],
 '03-mr-list': [(L, 95, 2880, 1290)],
 '04-mr3-overview': [(L, 95, 2880, 1800)],
 '05-mr3-diff': [(L, 95, 2880, 1800)],
 '06-issue1': [(L, 95, 2880, 1800)],
 '07-issue-list': [(L, 95, 2880, 820)],
 '08-releases': [(L, 95, 2880, 1800)],
 '10-milestones': [(L, 95, 2880, 420)],
 '11-admin-users': [(L, 440, 2880, 1640)],
 '12-doomed-archived': [(L, 95, 2880, 1300)],
 '13-branches': [(L, 95, 2880, 1000)],
 '14-tags': [(L, 95, 2880, 950)],
 '15-protected': [(L, 95, 2880, 1420)],
 '16-mr4-overview': [(L, 95, 2880, 1800)],
 '20-promoted-banner': [(L, 95, 2880, 1100)],
 '21-promoted-users': [(L, 95, 2880, 195), (L, 540, 2880, 1700)],
 '22-promoted-members': [(L, 95, 2880, 1350)],
 '30-offline-project': [(L, 95, 2880, 1250)],
 '31-offline-mr-list': [(L, 95, 2880, 1180)],
 '30-offline-project': [(L, 95, 2880, 1250)],
 '31-offline-mr-list': [(L, 95, 2880, 1180)],
}
only = set(sys.argv[1:])
for name, boxes in crops.items():
    if only and name not in only: continue
    try: src = Image.open(f'shots/{name}.png').convert('RGB')
    except FileNotFoundError: continue
    parts = [src.crop(b) for b in boxes]
    out = Image.new('RGB', (max(p.width for p in parts), sum(p.height for p in parts)), 'white')
    y = 0
    for p in parts: out.paste(p, (0, y)); y += p.height
    out.save(f'img/{name}.png', optimize=True)
    print(name, out.size)
