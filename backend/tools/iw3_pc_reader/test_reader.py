"""Structural reader regression tests (GPL-3.0)."""
import struct
import unittest
from reader import Reader, StreamError

F=0xffffffff

def zone(types,payload,virtual_bytes,temp_bytes=148):
    pool=b''.join(struct.pack('<II',t,ptr) for t,ptr in types)
    body=struct.pack('<4I',0,0,len(types),F)+pool+payload
    blocks=[temp_bytes,0,0,0,virtual_bytes,0,0,0,0]
    return struct.pack('<11I',len(body),0,*blocks)+body

class StructuralReaderTests(unittest.TestCase):
    def test_unknown_shader_name_is_not_filtered(self):
        name=b'effect_custom_program_947\0'
        z=zone([(5,F)],struct.pack('<I',F)+bytes(144)+name,8+len(name))
        r=Reader(z).run()
        self.assertEqual(r['assets'][0]['name'],'effect_custom_program_947')
        self.assertTrue(r['persistent_blocks_exact'])
        self.assertEqual(r['stream_end'],len(z))

    def test_signature_inside_raw_payload_is_not_an_asset(self):
        name=b'maps/mp/test.gsc\0';data=struct.pack('<I',F)+bytes(144)+b'fake_technique\0'
        payload=struct.pack('<III',F,len(data),F)+name+data+b'\0'
        z=zone([(31,F)],payload,8+len(name)+len(data)+1)
        r=Reader(z).run()
        self.assertEqual(r['asset_count'],1)
        self.assertEqual(len(r['objects']),1)

    def test_top_level_alias_resolves_previous_header(self):
        payload=struct.pack('<III',F,0,F)+b'test\0\0'
        z=zone([(31,F),(31,0x40000005)],payload,22)
        r=Reader(z).run()
        self.assertEqual(r['assets'][0]['root'],r['assets'][1]['root'])
        self.assertEqual(r['packed_references'],1)

    def test_non_temp_stringtable_reference_is_direct(self):
        payload=struct.pack('<4I',F,0,0,0)+b'a.csv\0'
        z=zone([(32,F),(32,0x40000011)],payload,38)
        r=Reader(z).run()
        self.assertEqual(r['assets'][0]['root'],r['assets'][1]['root'])
        self.assertEqual(r['assets'][1]['name'],'a.csv')

    def test_asset_alias_cannot_change_type(self):
        payload=struct.pack('<III',F,0,F)+b'test\0\0'
        z=zone([(31,F),(5,0x40000005)],payload,22)
        with self.assertRaisesRegex(StreamError,'Asset alias type mismatch'):
            Reader(z).run()

    def test_invalid_pointer_reports_asset_and_field(self):
        z=zone([(5,F)],struct.pack('<I',0x90000001)+bytes(144),8)
        with self.assertRaisesRegex(StreamError,r'Asset #0.*MaterialTechniqueSet.name.*Invalid packed pointer'):
            Reader(z).run()

    def test_unsupported_type_is_explicit(self):
        z=zone([(127,F)],b'',8)
        with self.assertRaisesRegex(StreamError,'Unsupported XAsset type 127, asset #0'):
            Reader(z).run()

    def test_truncated_payload_is_rejected(self):
        payload=struct.pack('<III',F,20,F)+b'test\0short'
        z=zone([(31,F)],payload,200)
        with self.assertRaisesRegex(StreamError,'Serialized read outside zone'):
            Reader(z).run()

    def test_declared_stream_end_is_checked(self):
        payload=struct.pack('<III',F,0,F)+b'test\0\0'
        z=bytearray(zone([(31,F)],payload,14));struct.pack_into('<I',z,0,len(z)-43)
        with self.assertRaisesRegex(StreamError,'header declares'):
            Reader(bytes(z)).run()

    def test_stringtable_pointer_array_and_null_cells(self):
        # Virtual block: asset pool 8, table root 16, name 6, aligned cells at 32.
        root=struct.pack('<4I',F,2,2,F);name=b'a.csv\0'
        cells=struct.pack('<4I',F,0,F,0x40000031)
        payload=root+name+cells+b'one\0two\0'
        z=zone([(32,F)],payload,56)
        r=Reader(z).run();table=r['assets'][0]['root'];p=r['pointers'][table+12]
        self.assertEqual([r['pointers'][p+i*4] for i in range(4)],['one',None,'two','one'])
        self.assertTrue(r['persistent_blocks_exact'])

if __name__=='__main__':unittest.main()
