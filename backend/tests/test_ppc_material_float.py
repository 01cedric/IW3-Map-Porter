"""Execute tiny instruction fixtures without requiring a game executable."""
import math
from pathlib import Path
import struct
import sys
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/ps3_loader_emulator'))
elf = ModuleType('ps3elf')
sys.modules['ps3elf'] = elf
from ppcemu import Cpu, Mem

m = Mem(); c = Cpu(m, {})
entry = 0x10000000
c.allowed = [(entry, entry + 0x1000)]

def run(words):
    m.write(entry, b''.join(struct.pack('>I', w) for w in words + [0x4e800020]))
    c.run(entry, [], maxsteps=20)

def load(op, reg, base, displacement=0):
    return (op << 26) | (reg << 21) | (base << 16) | (displacement & 0xffff)

def cmp(field, a, b):
    return (63 << 26) | (field << 23) | (a << 16) | (b << 11)

c.r[9] = 0x20004
m.write(0x20000, struct.pack('>ff', -1.25, 3.5))
run([load(48, 13, 9, -4), load(48, 12, 9), cmp(7, 13, 12)])
assert c.f[13] == -1.25 and c.f[12] == 3.5 and c.cr & 15 == 8
run([cmp(7, 12, 13)])
assert c.cr & 15 == 4
c.f[12] = -0.0; c.f[13] = 0.0
run([cmp(6, 12, 13)])
assert (c.cr >> 4) & 15 == 2 and c.cr & 15 == 4
c.f[12] = math.nan
run([cmp(7, 12, 13)])
assert c.cr & 15 == 1
c.r[0] = 0xBAD00000
m.write(0x100, struct.pack('>d', math.inf))
run([load(50, 0, 0, 0x100)])
assert c.f[0] == math.inf
# maxsteps is a per-call allowance, not a lifetime counter shared by all zones.
for _ in range(30): run([])
print('PASS: lfs/lfd, fcmpu ordered/unordered, CR isolation and per-call step limit')
