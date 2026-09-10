"""Desktop budget defaults, manual compatibility, and conversion handoff."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui_bridge
from cod4porter.ps3_budget import DEFAULT_ZONE_BUDGET_BYTES


class AutomaticBudget(unittest.TestCase):
    def test_default_and_migrated_desktop_settings(self):
        for settings in ({}, {'automatic_zone_budget': True, 'zone_budget_bytes': 'off'},
                         {'automatic_zone_budget': True, 'zone_budget_bytes': '90000000'}):
            with self.subTest(settings=settings):
                self.assertEqual(gui_bridge.budget_of(settings), DEFAULT_ZONE_BUDGET_BYTES)

    def test_explicit_manual_and_legacy_api_choices(self):
        for automatic in ({}, {'automatic_zone_budget': False}):
            self.assertIsNone(gui_bridge.budget_of(dict(automatic, zone_budget_bytes='off')))
            self.assertEqual(gui_bridge.budget_of(dict(automatic, zone_budget_bytes='90000000')), 90000000)

    def test_invalid_mode_and_manual_limit_rejected(self):
        for mode in ('false', None, 1):
            with self.assertRaises(ValueError):
                gui_bridge.budget_of({'automatic_zone_budget': mode})
        for budget in ('0', '-1', 'invalid'):
            with self.assertRaises(ValueError):
                gui_bridge.budget_of({'automatic_zone_budget': False, 'zone_budget_bytes': budget})

    def test_port_receives_budget_and_logs_actual_reductions(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'mp_test.ff'
            source.write_bytes(b'fixture')
            result = {'report_path': str(Path(temp) / 'report.json'), 'main_output': str(source),
                      'ps3_zone_allocation': {'declared_zone_bytes': 1234},
                      'ps3_zone_budget_plan': {'scaled_images': 7}}
            wire = io.StringIO()
            with patch('cod4porter.porter.port_map', return_value=result) as port, \
                    patch.object(gui_bridge, 'verify', return_value=('unproven', 'fixture')), \
                    patch.object(gui_bridge, 'WIRE', wire):
                gui_bridge.run({'schema_version': 1, 'command': 'port', 'output_dir': temp,
                                'settings': {'pc_ff': str(source), 'map_name': 'mp_test',
                                             'require_load_file': False, 'automatic_zone_budget': True,
                                             'zone_budget_bytes': 'off'}})
            self.assertEqual(port.call_args.kwargs['zone_budget_bytes'], DEFAULT_ZONE_BUDGET_BYTES)
            self.assertEqual(port.call_args.kwargs['texture_max_edge'], 0)
            self.assertIn('7 textures reduced', wire.getvalue())
            self.assertIn('1234 / ' + str(DEFAULT_ZONE_BUDGET_BYTES), wire.getvalue())


if __name__ == '__main__':
    unittest.main()
