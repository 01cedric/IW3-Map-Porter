import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tools/ps3_loader_emulator'))
from entity_parser import parse_entities
from gui_materials import decode_bc, decode_texture, remap_rgba
from gui_scene import export_scene, load_dump
import gui_bridge


class Entities(unittest.TestCase):
    def test_mapents_angles_targets_and_missing_origins(self):
        rows = parse_entities('''// comment with { }
        {"classname" "worldspawn"}
        {"classname" "mp_dm_spawn" "origin" "1 2 3" "angle" "90"}
        {"classname" "script_model" "targetname" "tower" "origin" "5 6 7" "angles" "10 20 30"}
        {"classname" "light" "origin" "nan 2 3"}
        {"classname" "info_player_start" "origin" "1 2 3" "angle" "-1"}
        ''')
        self.assertEqual(len(rows), 5); self.assertIsNone(rows[0]['origin'])
        self.assertEqual(rows[1]['angles'], [0, 90, 0]); self.assertTrue(rows[1]['is_spawn'])
        self.assertEqual(rows[2]['targetname'], 'tower'); self.assertFalse(rows[2]['is_spawn'])
        self.assertIsNone(rows[3]['origin']); self.assertEqual(rows[4]['angles'], [-90, 0, 0])

    def test_quoted_braces_and_backslash(self):
        rows = parse_entities(r'{"classname" "script_model" "note" "a { b } c" "targetname" "a\"b" "origin" "1 2 3"}')
        self.assertEqual(rows[0]['properties']['note'], 'a { b } c')
        self.assertEqual(rows[0]['targetname'], 'a"b')


class Textures(unittest.TestCase):
    def test_linear_argb_preview_preserves_color_and_alpha(self):
        image=decode_texture(bytes([40,10,20,30]),1,1,0xA5)
        self.assertEqual(image.getpixel((0,0)),(10,20,30,40))
        with self.assertRaises(ValueError): decode_texture(bytes(3),1,1,0xA5)

    def test_rsx_identity_and_forced_channels(self):
        rgba = np.array([[[10, 20, 30, 40]]], np.uint8)
        np.testing.assert_array_equal(remap_rgba(rgba, 0xAAE4), rgba)
        # R mode zero, G mode one; unrelated selectors stay intact.
        payload = (0xAAE4 & ~(3 << 10) & ~(3 << 12)) | (1 << 12)
        np.testing.assert_array_equal(remap_rgba(rgba, payload), [[[0, 255, 30, 40]]])

    def test_real_bc1_red_block(self):
        block = struct.pack('<HHI', 0xF800, 0x0000, 0)
        image = decode_bc(block, 4, 4, 0x86)
        self.assertEqual(image.getpixel((1, 1)), (255, 0, 0, 255))
        with self.assertRaises(ValueError): decode_bc(block[:-1], 4, 4, 0x86)


class Scenes(unittest.TestCase):
    def test_binary_scene_entities_uv_and_indices(self):
        with tempfile.TemporaryDirectory() as temp:
            data = dict(pos=np.array([[10, 0, 0], [10, 1, 0], [10, 0, 1]], float),
                        indices=np.array([0, 1, 2]), surfaces=np.array([[0, 0, 0, 3, 1, 0, 7, 0, 0, 0, 0]]),
                        smodels=np.zeros((0, 11)), layers_raw=b''.join(bytes([255] * 4) + struct.pack('>2f', u, v) + bytes(16) for u, v in [(0, 0), (1, 0), (0, 1)]))
            meta = dict(materials={7: 'wc/test'}, entity_string='{"classname" "script_origin" "origin" "1 2 3"}')
            path = export_scene(data, meta, temp)
            report = json.loads(path.read_text()); self.assertEqual(report['entities'][0]['origin'], [1, 2, 3])
            raw = (Path(temp) / report['mesh_file']).read_bytes(); self.assertEqual(raw[:8], b'IW3SCN2\0')
            self.assertEqual(len(raw), 12 + 12 + 3 * 20 + 3 * 4)
            self.assertEqual(struct.unpack_from('<6f', raw, 24 + 36), (0, 0, 1, 0, 0, 1))
            data['indices'][2] = 3
            with self.assertRaises(ValueError): export_scene(data, meta, temp)

    def test_metadata_cannot_execute_pickle_globals(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'world.npz'; np.savez(path, pos=np.zeros((0, 3)))
            Path(str(path) + '.meta').write_bytes(b'cos\nsystem\n(S"echo unwanted"\ntR.')
            import pickle
            with self.assertRaises(pickle.UnpicklingError): load_dump(path)


class Bridge(unittest.TestCase):
    def test_options_are_exact_and_sound_is_omit(self):
        options = gui_bridge.common_options({'unresolved_image_policy': 'neutral', 'portal_policy': 'omit'})
        self.assertEqual(options['unresolved_image_policy'], 'neutral'); self.assertEqual(options['portal_policy'], 'omit')
        self.assertEqual(options['sound_policy'], 'omit')
        with self.assertRaises(ValueError): gui_bridge.common_options({'material_image_resource_policy': 'retail-delayed'})

    def test_terminal_failure_for_missing_input(self):
        with tempfile.TemporaryDirectory() as temp:
            request = Path(temp) / 'request.json'
            request.write_text(json.dumps(dict(schema_version=1, command='port', output_dir=temp,
                                              settings={'map_name': 'mp_test', 'pc_ff': '/missing.ff'})))
            wire = io.StringIO()
            with patch.object(sys, 'argv', ['gui_bridge', '--request', str(request)]), patch.object(gui_bridge, 'WIRE', wire):
                self.assertEqual(gui_bridge.main(), 2)
            rows = [json.loads(line) for line in wire.getvalue().splitlines()]
            self.assertEqual(rows[-1]['type'], 'result'); self.assertEqual(rows[-1]['status'], 'failed')
            self.assertTrue((Path(temp) / 'job-summary.json').is_file())

    def test_final_newline_mapname_is_rejected(self):
        with self.assertRaises(ValueError): gui_bridge.validate_input({'map_name': 'mp_test\n'}, 'analyze')


if __name__ == '__main__': unittest.main(verbosity=2)
