"""Regression: s0 graph substitution must not introduce missing envMapParms."""
from pathlib import Path
from types import SimpleNamespace
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools' / 'ps3_loader_emulator'))
from cod4porter.material_constants import bind_material_constants, CONTRACTS
from cod4porter.technique_binding import TechniqueBinding, TechniqueEvidence
from materialcheck import validate_material_constants
class Mem:
    def __init__(self): self.data=bytearray(0x20000)
    def write(self,p,v): self.data[p:p+len(v)]=v
    def w32(self,p,v): self.write(p,struct.pack('>I',v))
    def w16(self,p,v): self.write(p,struct.pack('>H',v))
    def u32(self,p): return struct.unpack_from('>I',self.data,p)[0]
    def u16(self,p): return struct.unpack_from('>H',self.data,p)[0]
    def u8(self,p): return self.data[p]

b = TechniqueBinding('mc_l_sm_t0c0', 'mc_l_sm_t0c0s0', False,
                     TechniqueEvidence.PS3_FAMILY_SUBSTITUTION, True, 'fixture')
color = struct.pack('<I', 0xb60c3b3a) + bytes(28)
env = struct.pack('<I', 0x3d9994dc) + bytes(28)
material = SimpleNamespace(name='roof_vent', constants=(color,))
result = bind_material_constants(material, b)
assert result.candidate_name != b.candidate_name
assert CONTRACTS[result.candidate_name.casefold()] <= {0xb60c3b3a}
assert bind_material_constants(SimpleNamespace(name='metal', constants=(color, env)), b) == b

m = Mem(); root=0x10000; techset=0x11000; technique=0x12000; table=0x13000; args=0x14000
m.w32(root+0x70,techset);m.write(root+0x33,b'\x01');m.w32(root+0x78,table)
m.w32(table,0xb60c3b3a);m.w32(techset+8,technique);m.w16(technique+6,1)
m.write(technique+8+14,b'\x01');m.w32(technique+8+20,args)
m.w16(args,6);m.w32(args+4,0x3d9994dc)
try:
    validate_material_constants(m,root,lambda _: 'fixture')
except RuntimeError as e:
    assert 'missing shader constant 0x3d9994dc' in str(e)
else:
    raise AssertionError('missing envMapParms accepted')
m.write(root+0x33,b'\x02');m.w32(table+32,0x3d9994dc)
validate_material_constants(m,root,lambda _: 'fixture')
print('PASS: compatible graph selection and native constant lookup guard')

# A material with all constants can still lack a required texture sampler.
m.w16(args,2);m.w32(args+4,0x12345678)
try:validate_material_constants(m,root,lambda _: 'fixture')
except RuntimeError as e:assert 'missing texture sampler 0x12345678' in str(e)
else:raise AssertionError('missing sampler accepted')
m.write(root+0x32,b'\x01');m.w32(root+0x74,0x15000);m.w32(0x15000,0x12345678)
validate_material_constants(m,root,lambda _: 'fixture')
