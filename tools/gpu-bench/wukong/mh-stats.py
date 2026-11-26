#!/usr/bin/env python3
"""MangoHud CSV -> frames, mean fps (frames / summed frame time), 1 % and 0.1 % low, from per-frame times."""
import csv, sys
rows = list(csv.reader(open(sys.argv[1], errors='replace')))
hi = next(i for i, r in enumerate(rows) if r and r[0].strip() == 'fps')
hdr = [h.strip() for h in rows[hi]]
ft = [float(r[hdr.index('frametime')]) for r in rows[hi + 1:] if len(r) == len(hdr)]
ft = [x for x in ft if x > 0]
ft_sorted = sorted(ft)
def low(p):
    k = max(1, int(len(ft) * p))
    worst = ft_sorted[-k:]
    return 1000.0 * len(worst) / sum(worst)
mean = 1000.0 * len(ft) / sum(ft)
print('%s frames=%d seconds=%.1f mean=%.2f low1=%.2f low0.1=%.2f' % (sys.argv[1].split('/')[-1], len(ft), sum(ft) / 1000, mean, low(0.01), low(0.001)))
