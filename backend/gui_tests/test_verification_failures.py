import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_bridge import verification_failures
from gui_emulator import linker_message

class VerificationFailures(unittest.TestCase):
    def test_structural_failure_does_not_blame_passing_preflight(self):
        rows = verification_failures({'structural_passed': False, 'structural_failures': [
            {'check_id': 'LOAD.ACTUAL_LEVEL_NAME', 'observed': 'wrong', 'expected': 'loadscreen_mp_unit', 'detail': 'identity'}]},
            {'checks': [{'key': 'LOAD.BLOCK_DECLARATION', 'passed': True}]}, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stage'], 'Structural audit')
        self.assertEqual(rows[0]['actual'], 'wrong')
        self.assertEqual(rows[0]['check'], 'LOAD.ACTUAL_LEVEL_NAME')

    def test_both_stages_preserve_details(self):
        rows = verification_failures({'structural_passed': False, 'structural_failures': [
            {'check_id': 'MAIN.READBACK', 'observed': False, 'expected': True}]},
            {'checks': [{'key': 'MAIN.FRAMING', 'passed': False, 'actual': 0, 'expected': '> 0', 'detail': 'checksum'}]}, 2)
        self.assertEqual([r['stage'] for r in rows], ['Structural audit', 'PS3 preflight'])
        self.assertEqual(rows[1]['actual'], 0)

    def test_missing_failure_details_are_not_success(self):
        rows = verification_failures({'structural_passed': True}, {}, 2)
        self.assertEqual(rows[0]['check'], 'REPORT_UNAVAILABLE')
        self.assertEqual(rows[0]['stage'], 'PS3 preflight')

class LinkerMessages(unittest.TestCase):
    def test_default_assets_and_unexecuted_scripts_remain_visible_in_cache(self):
        report={'status':'ok','externals_missing':[['image','missing','$white']],
                'script_validation':{'missing_script_references':[], 'missing_literal_assets':[
                    {'asset_type':9,'name':'sound'}, {'asset_type':9,'name':'SOUND'}]}}
        for cached in (False,True):
            message=linker_message(report,cached)
            self.assertIn('1 native references use default assets',message)
            self.assertIn('1 unavailable asset names',message)
            self.assertIn('remain unverified',message)
    def test_linker_error_is_shown(self):
        self.assertIn('missing shader constant',linker_message({'status':'emulation_error','error':'missing shader constant'}))

if __name__ == '__main__': unittest.main()
