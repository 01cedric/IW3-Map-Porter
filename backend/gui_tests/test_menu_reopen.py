import copy
import os
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_mapmenu
from gui_menu_reopen import pack_state, unpack_state
from gui_ui_relocation import rebuild


class ReopeningTests(unittest.TestCase):
    def test_recovery_restores_grown_strings_and_relocated_pointers(self):
        zone, profile = test_mapmenu.RelocationTests().fixture()
        recovery = {}
        out, _ = rebuild(zone, profile, {52: b'a'*20+b'\0'}, recovery=recovery)
        rows = [dict(slot=1, map_id='mp_test', display_name='Test',
                     description='Description', preview_load_ff='local-file.ff')]
        trailer = pack_state(out, zone, recovery, rows, b'original trailer')
        original, restored, tail = unpack_state(SimpleNamespace(zone=out, trailer=trailer))
        self.assertEqual(original, zone)
        self.assertEqual(tail, b'original trailer')
        self.assertEqual(restored[0]['description'], 'Description')
        self.assertNotIn('preview_load_ff', restored[0])
        with self.assertRaisesRegex(ValueError, 'do not match'):
            unpack_state(SimpleNamespace(zone=out+b'x', trailer=trailer))
        with self.assertRaisesRegex(ValueError, 'length'):
            unpack_state(SimpleNamespace(zone=out, trailer=trailer[:-1]))

    def test_nested_clones_keep_their_own_pointer_targets(self):
        zone, profile = test_mapmenu.RelocationTests().fixture()
        # Two copies reuse the same source offsets but require distinct targets.
        child = dict(events=profile['events'][1:-1], replacements={},
                     fixups=profile['fixups'])
        parent = dict(events=[[6, child], [6, copy.deepcopy(child)]],
                      replacements={}, fixups=[], kind='container')
        out, _ = rebuild(zone, profile, inserts={5: [parent]})
        self.assertEqual(struct.unpack_from('>2I', out, 68), (0x80000019, 0x80000025))
        self.assertEqual(struct.unpack_from('>2I', out, 80), (0x80000029, 0x80000035))


@unittest.skipUnless(os.environ.get('IW3_MENU_EXPORT'), 'Exported PS3 UI fixture is external.')
class ExportReopeningTests(unittest.TestCase):
    def test_reopen_and_reexport_preserves_names_descriptions_and_pixels(self):
        from cod4porter.backend.v4_ffio import read_ps3_fastfile
        from gui_menu_reopen import open_source
        from gui_mapmenu import execute
        doc = read_ps3_fastfile(os.environ['IW3_MENU_EXPORT'])
        original, _, rows, images, _, baseline = open_source(doc)
        self.assertEqual(baseline, '')
        self.assertEqual(len(images), len(rows)-16)
        self.assertTrue(all(row['preview_load_ff'] == '[Embedded in UI]' for row in rows[16:]))
        with tempfile.TemporaryDirectory() as tmp:
            execute('mapmenu-write', dict(map_menu_ff=str(doc.path), map_menu_entries=rows),
                    Path(tmp), lambda *args: None, lambda *args: None)
            result = read_ps3_fastfile(Path(tmp)/'ui_mp.ff')
            recovered = open_source(result)
            self.assertEqual(recovered[0], original)
            self.assertEqual(recovered[2], rows)
            self.assertEqual(recovered[3], images)
            self.assertEqual(result.zone, doc.zone)

    def test_legacy_export_requires_original_and_rejects_modified_assets(self):
        if not os.environ.get('IW3_MENU_LEGACY') or not os.environ.get('IW3_MENU_FF'):
            self.skipTest('Legacy UI and original fixtures are external.')
        from cod4porter.backend.v4_ffio import read_ps3_fastfile
        from gui_menu_reopen import open_source
        doc = read_ps3_fastfile(os.environ['IW3_MENU_LEGACY'])
        with self.assertRaisesRegex(ValueError, 'Original UI'):
            open_source(doc)
        original, _, rows, images, _, _ = open_source(doc, os.environ['IW3_MENU_FF'])
        self.assertGreater(len(rows), 16)
        self.assertEqual(len(images), len(rows)-16)
        zone = bytearray(doc.zone); zone[900] ^= 1
        with self.assertRaises(ValueError):
            open_source(SimpleNamespace(zone=bytes(zone), trailer=doc.trailer), os.environ['IW3_MENU_FF'])


if __name__ == '__main__':
    unittest.main()
