import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_inputs import load_companion

class CompanionTests(unittest.TestCase):
    def test_matching_and_numbered_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            for stem in ('mp_test','mp_test(7)'):
                main=Path(folder)/(stem+'.ff');main.touch()
                s={'ps3_ff':str(main)}
                self.assertIsNone(load_companion(s))
                name='mp_test_load'+('(7)' if '(7)' in stem else '')+'.ff'
                companion=Path(folder)/name;companion.touch()
                self.assertEqual(load_companion(s),companion)
                self.assertIsNone(load_companion({'ps3_ff':str(companion)}))

    def test_explicit_missing_is_an_error(self):
        with self.assertRaises(ValueError):
            load_companion({'ps3_ff':'test.ff','ps3_load_ff':'missing/load.ff'})

if __name__=='__main__':unittest.main()
