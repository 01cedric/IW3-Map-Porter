import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gui_mapmenu import read_profile,inspect_zone,prepare_edit,START_ACTION
from gui_menu_previews import add_previews
from cod4porter.backend.v4_ffio import read_ps3_fastfile

@unittest.skipUnless(os.environ.get('IW3_MENU_FF'),'Original PS3 UI is external.')
class MenuPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.zone=read_ps3_fastfile(os.environ['IW3_MENU_FF']).zone
        cls.profile=read_profile(cls.zone)

    def test_missing_loading_picture_rejects_export(self):
        rows=inspect_zone(self.zone,self.profile)
        rows.append(dict(slot=17,map_id='mp_custom',display_name='Custom'))
        changes,inserts,_=prepare_edit(self.zone,self.profile,rows)
        with self.assertRaisesRegex(ValueError,'converted PS3 _load.ff'):
            add_previews(self.zone,self.profile,rows,changes,inserts)

    def test_mismatched_loading_zone_is_rejected(self):
        rows=inspect_zone(self.zone,self.profile)
        rows.append(dict(slot=17,map_id='mp_custom',display_name='Custom',preview_load_ff=str(Path(__file__).resolve().parents[1]/'reference/mp_getaway_load_version09.ff')))
        changes,inserts,_=prepare_edit(self.zone,self.profile,rows)
        with self.assertRaisesRegex(ValueError,'belongs to'):
            add_previews(self.zone,self.profile,rows,changes,inserts)

    def test_start_script_fits_native_buffer_at_maximum_id_lengths(self):
        rows=inspect_zone(self.zone,self.profile)
        for i in range(16):rows.append(dict(slot=i+17,map_id='mp_'+('x'*58)+f'{i:02}',display_name='Custom '+str(i)))
        changes,_,_=prepare_edit(self.zone,self.profile,rows)
        self.assertLessEqual(len(changes[START_ACTION]),0x1400)
        self.assertIn(b'set iw3_menu_launch xpartygo',changes[START_ACTION])
        self.assertIn(b'setfromdvar g_gametype ui_gametype',changes[START_ACTION])

if __name__=='__main__':unittest.main()
