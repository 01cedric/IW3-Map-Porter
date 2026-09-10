"""Conversion verification must check its output, preserve blockers and disclose skips."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gui_bridge import native_verification, verify
from gui_emulator import SUPPORT_ORDER


class AutomaticChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.settings = dict(elf_path=str(self.out / 'target.elf'), support_directory=str(self.out),
                             map_name='mp_previous', ps3_ff='previous.ff', ps3_load_ff='previous_load.ff')
        for name in ('target.elf', *(n + '.ff' for n in SUPPORT_ORDER)):
            (self.out / name).touch()
        for target in ('gui_bridge.emit', 'gui_bridge.artifact'):
            mock = patch(target); mock.start(); self.addCleanup(mock.stop)

    def run_check(self, **kwargs):
        return native_verification('mp_current', self.out / 'mp_current.ff',
            self.out / 'mp_current_load.ff', kwargs.pop('settings', self.settings), self.out, **kwargs)

    def coverage(self):
        return json.loads((self.out / 'mp_current.check-coverage.json').read_text())

    @patch('gui_emulator.execute', return_value=('unproven', 'Native checks completed.'))
    def test_checks_current_output_without_building_preview(self, execute):
        self.run_check()
        command, settings, *_ = execute.call_args.args
        self.assertEqual(command, 'validate')
        self.assertEqual(settings['map_name'], 'mp_current')
        self.assertEqual(settings['ps3_ff'], str(self.out / 'mp_current.ff'))
        self.assertEqual(settings['ps3_load_ff'], str(self.out / 'mp_current_load.ff'))
        self.assertEqual(self.settings['map_name'], 'mp_previous')
        self.assertEqual(self.coverage()['status'], 'completed')
        self.assertFalse(self.coverage()['game_startup_tested'])

    @patch('gui_emulator.execute')
    def test_missing_prerequisites_are_explicit(self, execute):
        status, message = self.run_check(settings={})
        execute.assert_not_called()
        self.assertEqual(status, 'unproven')
        self.assertIn('skipped', message)
        self.assertEqual(len(self.coverage()['missing']), 4)

    @patch('gui_emulator.execute')
    def test_structural_blocker_prevents_native_execution(self, execute):
        self.assertEqual(self.run_check(blocked=True)[0], 'failed')
        execute.assert_not_called()
        self.assertEqual(self.coverage()['reason'], 'blocked_by_failed_audit')

    @patch('gui_emulator.execute', side_effect=ValueError('ELF profile mismatch'))
    def test_native_exception_is_reported_as_failure(self, execute):
        self.assertEqual(self.run_check()[0], 'failed')
        self.assertEqual(self.coverage()['error'], 'ELF profile mismatch')

    @patch('gui_emulator.execute', return_value=('failed', 'World bounds failed'))
    def test_native_failure_propagates_through_verify(self, execute):
        report = self.out / 'mp_current.port.json'
        report.write_text(json.dumps(dict(map='mp_current', load={})))
        (self.out / 'mp_current.ff').touch()
        (self.out / 'mp_current_load.ff').touch()
        with patch('tools.c11_audit.audit', return_value=dict(structural_passed=True, full_fidelity_passed=False,
                   structural_passed_count=1, structural_total=1)), \
             patch('tools.ps3_preflight.main', return_value=0):
            status, message = verify(report, self.settings, self.out)
        self.assertEqual((status, message), ('failed', 'World bounds failed'))


if __name__ == '__main__': unittest.main()
