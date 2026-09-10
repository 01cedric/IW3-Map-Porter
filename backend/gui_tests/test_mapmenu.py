import os
import struct
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_ui_relocation import rebuild
from gui_mapmenu import read_profile, inspect_zone, prepare_edit
from cod4porter.backend.v4_ffio import read_ps3_fastfile


class RelocationTests(unittest.TestCase):
    def fixture(self):
        # A string followed by an aligned pointer record, including a reference
        # into the second allocation. Logical padding is not serialized padding.
        zone = bytearray(36)
        zone += bytes(16) + b'abc\0' + struct.pack('>2I', 0x80000001, 0x80000015)
        struct.pack_into('>9I', zone, 0, 24, 0, 0, 0, 0, 0, 24, 0, 0)
        profile = dict(events=[[0,4],[2,0],[3,0x60000000,52,4],
                               [2,15],[3,0x60000010,56,8],[1]],
                       fixups=[[56,0x80000001,'pointer'],[60,0x80000015,'pointer']],
                       high=[0,0,0,0,24,0,0],stream_end=64)
        return bytes(zone), profile

    def test_growth_moves_aligned_target_and_pointer_fields(self):
        zone, profile = self.fixture()
        out, report = rebuild(zone, profile, {52: b'a'*20+b'\0'})
        self.assertEqual(struct.unpack_from('>2I',out,73),(0x80000001,0x80000025))
        self.assertEqual(report['declared_blocks'][4],40)
        self.assertEqual(len(out),81)  # no artificial physical alignment padding
        # The string grows by 17 serialized bytes but only 16 logical bytes.
        self.assertEqual(struct.unpack_from('>I',out)[0],41)

    def test_edited_alias_relocates_new_target(self):
        zone, profile = self.fixture()
        out, _ = rebuild(zone, profile, {52:b'a'*20+b'\0',56:struct.pack('>2I',0x80000011,0x80000015)})
        self.assertEqual(struct.unpack_from('>2I',out,73),(0x80000021,0x80000025))

    def test_unchanged_roundtrip(self):
        zone, profile = self.fixture()
        self.assertEqual(rebuild(zone,profile)[0],zone)

    def test_rejects_pointer_into_removed_string_bytes(self):
        zone, profile = self.fixture()
        profile['fixups'][0][1] = 0x80000004
        zone=bytearray(zone);struct.pack_into('>I',zone,56,0x80000004)
        with self.assertRaisesRegex(ValueError,'outside'):
            rebuild(zone,profile,{52:b'x\0'})


@unittest.skipUnless(os.environ.get('IW3_MENU_FF'), 'Original PS3 UI fixture is external.')
class RetailMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zone = read_ps3_fastfile(os.environ['IW3_MENU_FF']).zone
        cls.profile = read_profile(cls.zone)

    def test_native_profile_noop_is_byte_exact(self):
        self.assertEqual(rebuild(self.zone,self.profile)[0],self.zone)

    def test_extended_name_and_32_choices(self):
        rows=inspect_zone(self.zone,self.profile)
        rows[0]['display_name']='Ambush with a longer display name'
        for i in range(16):
            rows.append(dict(slot=17+i,map_id=f'mp_custom_{i}',display_name='Custom '+str(i)))
        replacements,inserts,edits=prepare_edit(self.zone,self.profile,rows)
        out,report=rebuild(self.zone,self.profile,replacements,inserts)
        self.assertEqual(report['added_rows'],16)
        self.assertEqual(len(edits),17)
        self.assertIn(b'Ambush with a longer display name\0',out)
        self.assertIn(b'"mp_custom_15"',out)
        self.assertGreater(report['allocations'],536938)

    def test_unknown_layout_and_script_injection_are_rejected(self):
        bad=bytearray(self.zone);bad[100]^=1
        with self.assertRaises(ValueError):read_profile(bad)
        rows=inspect_zone(self.zone,self.profile)
        rows[0]['display_name']='Name"; exec quit'
        with self.assertRaises(ValueError):prepare_edit(self.zone,self.profile,rows)

    def test_custom_submenu_accepts_sixteen_added_maps(self):
        from gui_menu_submenu import prepare_submenu
        rows = inspect_zone(self.zone, self.profile)
        for i in range(16):
            rows.append(dict(slot=17+i, map_id=f'mp_custom_{i}', display_name=f'Custom {i}'))
        replacements, inserts, _ = prepare_submenu(self.zone, self.profile, rows)
        submenu = next(c for scopes in inserts.values() for c in scopes if c.get('kind') == 'custom-menu')
        self.assertEqual(struct.unpack_from('>I', replacements[976], 4)[0], 332)
        self.assertEqual(struct.unpack_from('>I', replacements[0x14B9975], 0xb0)[0], 181)
        self.assertEqual(struct.unpack_from('>I', submenu['replacements'][0x14B9975], 0xb0)[0], 239)
        out, _ = rebuild(self.zone, self.profile, replacements, inserts)
        self.assertIn(b'^3Custom Maps^7\0', out)
        self.assertIn(b'"mp_custom_15"', out)
        rows.append(dict(slot=33, map_id='mp_over_limit', display_name='Over limit'))
        with self.assertRaisesRegex(ValueError, '32 map choices'):
            prepare_submenu(self.zone, self.profile, rows)

    def test_export_growth_preserves_assets_and_fills_final_frame(self):
        import tempfile
        from gui_mapmenu import execute
        rows=inspect_zone(self.zone,self.profile)
        rows[0]['display_name']='Ambush with a longer display name'
        replacements,inserts,_=prepare_edit(self.zone,self.profile,rows)
        expected,_=rebuild(self.zone,self.profile,replacements,inserts)
        original_tail=len(self.zone)-self.profile['stream_end']
        self.assertEqual(struct.unpack_from('>I',expected)[0],len(expected)-original_tail-36)
        self.assertNotEqual(len(expected)%65536,0)
        with tempfile.TemporaryDirectory() as tmp:
            execute('mapmenu-write',dict(map_menu_ff=os.environ['IW3_MENU_FF'],map_menu_entries=rows),
                    Path(tmp),lambda *args:None,lambda *args:None)
            result=read_ps3_fastfile(Path(tmp)/'ui_mp.ff')
        self.assertEqual(result.zone[:len(expected)],expected)
        self.assertEqual(result.zone[len(expected):],bytes((-len(expected))&65535))
        self.assertTrue(all(b.raw_bytes==65536 and (not b.compressed or b.adler32_verified) for b in result.blocks))


if __name__ == '__main__':unittest.main()
