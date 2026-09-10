#!/usr/bin/env python3
"""Load the always-loaded PS3 zones once with real DB linking and snapshot the machine."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dbworld import Machine, ComError
from linkcheck import SUPPORT_ORDER
support_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(support_dir, 'support_db.snapshot')
M = Machine(real_link=True); M.db_init()
for name in SUPPORT_ORDER:
    z = open(os.path.join(support_dir, 'zone_%s.bin' % name), 'rb').read()
    t = time.time()
    zl = M.load_zone(z, name)
    print('%-18s %5d assets  %6.1fs  lookups so far %d' % (name, len(zl.assets), time.time() - t, len(M.lookups)))
    sys.stdout.flush()
M.save(out)
print('snapshot written:', out, os.path.getsize(out) // 1024, 'KiB')
misses = [(hex(lr), M.type_names[t], n) for lr, t, n in M.lookups]
print('DB_FindXAssetHeader calls during support load:', len(misses))
for m in misses[:20]: print('  ', m)
