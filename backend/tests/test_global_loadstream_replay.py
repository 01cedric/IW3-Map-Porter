#!/usr/bin/env python3
from cod4porter import global_loadstream_replay as m

# FOLLOWING images must not consume alias cells; 23 bad cells explain exactly 92 bytes.
r=m.Replay(pointer_size=4,alias_block=4); r.set_block(4,1000)
for i in range(23): r.allocate_following(block=1,size=16,alignment=4,label=f"f{i}",owner="gfx",key=f"i{i}")
assert r.cursor(4).offset==1000
for i in range(23): r.allocate_insert(block=1,size=16,alignment=4,label=f"x{i}",owner="gfx",key=f"x{i}")
assert r.cursor(4).offset==1092
# Alias resolution is exact and cycles/conflicts are rejected.
t,a=r.allocate_insert(block=1,size=8,alignment=4,label="sky",owner="gfxworld",key="sp_bonus_ft")
assert r.resolve(a)==(t,"sp_bonus_ft")
try:r.resolve(m.VirtualAddress(4,999999))
except m.ReplayError:pass
else:raise AssertionError("unproved address resolved")
print("PASS: deterministic global loadstream replay")
