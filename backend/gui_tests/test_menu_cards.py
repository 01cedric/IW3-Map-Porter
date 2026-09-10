import os, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gui_menu_cards import CREDIT,description_text

class DescriptionTests(unittest.TestCase):
    def test_wrapping_and_newline_normalization(self):
        self.assertEqual(description_text(' Hello\r\nWorld '),'Hello\nWorld')
        self.assertLessEqual(len(description_text('a'*160).splitlines()),4)

    def test_rejects_text_that_would_overlap_credit(self):
        for text in ('a'*161,'a\nb\nc\nd\ne','text\0hidden','^1red'):
            with self.assertRaises(ValueError):description_text(text)

    def test_quotes_are_literal_description_content(self):
        self.assertEqual(description_text('A "snowy" map; close quarters.'),'A "snowy" map; close quarters.')

@unittest.skipUnless(os.environ.get('IW3_MENU_FF'),'Original UI is external.')
class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gui_mapmenu import read_profile
        from cod4porter.backend.v4_ffio import read_ps3_fastfile
        cls.zone=read_ps3_fastfile(os.environ['IW3_MENU_FF']).zone;cls.profile=read_profile(cls.zone)

    def test_description_only_edit_and_fixed_credit(self):
        from gui_mapmenu import inspect_zone,prepare_edit
        rows=inspect_zone(self.zone,self.profile);rows[0]['description']='Test description';rows[0]['credit']='Replace this credit'
        changes,inserts,edits=prepare_edit(self.zone,self.profile,rows)
        cards=[c for scopes in inserts.values() for c in scopes if c.get('kind')=='map-card']
        self.assertEqual(len(cards),4);self.assertEqual(len(edits),1)
        raw=b''.join(bytes.fromhex(e[1]) for c in cards for e in c['events'] if e[0]==5)
        self.assertIn(CREDIT.encode()+b'\0',raw);self.assertNotIn(b'Replace this credit',raw)
        self.assertIn(b'Test description\0',raw)
