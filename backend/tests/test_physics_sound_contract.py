"""Reject the console-observed NULL sound prefix after native asset linking."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/ps3_loader_emulator'))
from physicscheck import validate_physics_sound_prefixes

class Memory:
    pointer=0
    payload=b''
    def u32(self,address):
        assert address==0x1001c
        return self.pointer
    def u8(self,address):return self.payload[address-0x20000]

m=Memory();inventory={(1,'test_preset'):0x10000,(4,'irrelevant_material'):0}
try:validate_physics_sound_prefixes(m,inventory)
except RuntimeError as e:assert 'test_preset' in str(e) and 'NULL sndAliasPrefix' in str(e)
else:raise AssertionError('NULL prefix accepted')
for pointer in (4,0xfffffffe,0xffffffff):
    m.pointer=pointer
    try:validate_physics_sound_prefixes(m,inventory)
    except RuntimeError as e:assert 'invalid linked' in str(e)
    else:raise AssertionError('Invalid linked prefix accepted')
m.pointer=0x20000
for payload in (b'\0',b'metal\0'):
    m.payload=payload
    report=validate_physics_sound_prefixes(m,inventory)
    assert report['passed'] and report['checked']==1
    assert report['empty_prefixes']==(['test_preset'] if payload==b'\0' else [])
m.payload=b'x'*4096
try:validate_physics_sound_prefixes(m,inventory)
except RuntimeError as e:assert 'terminator' in str(e)
else:raise AssertionError('Unterminated prefix accepted')
print('PASS: NULL PhysPreset prefix rejected; empty and named prefixes accepted')
