import json
from pathlib import Path
import struct
import tempfile
import unittest
from gui_revision import read_sfo, installation_metadata, normalize_title_id, inspect_elf

def sfo(values):
    keys=bytearray();data=bytearray();rows=[]
    for k,v in values.items():
        raw=v.encode()+b'\0';rows.append(struct.pack('<HHIII',len(keys),0x204,len(raw),len(raw),len(data)))
        keys+=k.encode()+b'\0';data+=raw
    start=20+16*len(rows)
    return b'\0PSF'+struct.pack('<4I',0x101,start,start+len(keys),len(rows))+b''.join(rows)+keys+data

def elf(duplicate=False):
    b=bytearray(0x400)
    b[:6]=b'\x7fELF\x02\x02';struct.pack_into('>H',b,18,21)
    struct.pack_into('>QQ',b,24,0x10100,64);struct.pack_into('>HH',b,54,56,1)
    struct.pack_into('>IIQQQQQQ',b,64,1,7,0,0x10000,0x10000,len(b),len(b),0x10000)
    struct.pack_into('>II',b,0x100,0x10120,0x10200)
    struct.pack_into('>II',b,0x120,0x80820000,0x4800001d)
    if duplicate:struct.pack_into('>II',b,0x150,0x80820000,0x4bffffed)
    struct.pack_into('>I',b,0x200,0x10300)
    text=b"Couldn't find the bsp for this map.\0";b[0x300:0x300+len(text)]=text
    return b

class RevisionTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_title_id(' blus-30072 '),'BLUS30072')
        with self.assertRaises(ValueError):normalize_title_id('BLUS30072P')
    def test_disc_and_update_are_read_not_assumed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'USRDIR';p.mkdir();exe=p/'EBOOT.ELF';exe.write_bytes(b'')
            self.assertIsNone(installation_metadata(exe)['app_version'])
            (root/'PARAM.SFO').write_bytes(sfo(dict(TITLE_ID='BLJS10013',APP_VER='01.00',CATEGORY='DG')))
            r=installation_metadata(exe);self.assertEqual(r['source_kind'],'disc');self.assertFalse(r['update_1_40_metadata'])
            (root/'PARAM.SFO').write_bytes(sfo(dict(TITLE_ID='BLUS30072',APP_VER='01.40',CATEGORY='GD')))
            r=installation_metadata(exe);self.assertTrue(r['update_1_40_metadata']);self.assertFalse(r['running_executable_verified'])
    def test_sfo_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'PARAM.SFO';b=bytearray(sfo(dict(TITLE_ID='BLUS30072')));struct.pack_into('<I',b,32,0xffffffff);p.write_bytes(b)
            with self.assertRaises(ValueError):read_sfo(p)
    def test_unique_call_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.elf';p.write_bytes(elf());r=inspect_elf(p)
            self.assertEqual(r['bsp_error_breakpoints'][0]['address'],'0x00010124')
            p.write_bytes(elf(True));r=inspect_elf(p);self.assertEqual(r['bsp_error_breakpoints'],[]);self.assertEqual(r['candidate_count'],2)
    def test_missing_string_and_encrypted(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.elf';b=elf();b[0x300:0x330]=bytes(48);p.write_bytes(b)
            self.assertEqual(inspect_elf(p)['bsp_error_breakpoints'],[])
            p.write_bytes(b'SCE\0'+bytes(200))
            with self.assertRaises(ValueError):inspect_elf(p)

if __name__=='__main__':unittest.main()
