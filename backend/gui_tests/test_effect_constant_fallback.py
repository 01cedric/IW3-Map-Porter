import struct
import unittest
from types import SimpleNamespace
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.material_constants import bind_material_constants, CONTRACTS
from cod4porter.technique_binding import TechniqueBinding, TechniqueEvidence


class EffectConstantFallbackTests(unittest.TestCase):
    def binding(self, source):
        return TechniqueBinding(source, 'effect_zfeather_add', False,
                                TechniqueEvidence.PS3_FAMILY_SUBSTITUTION, True,
                                'Initial availability substitution')

    def test_eyeoffset_retains_source_family_siblings(self):
        material = SimpleNamespace(name='gfx_spotlight_lensflare_eyeoffset', constants=())
        for source in ('effect_add_eyeoffset', 'sm2/effect_add_eyeoffset', ',sm2/effect_add_eyeoffset'):
            result = bind_material_constants(material, self.binding(source))
            self.assertEqual(result.candidate_name, 'effect_add_nofog')
            self.assertEqual(CONTRACTS[result.candidate_name], frozenset())
            self.assertIn('no synthetic', result.reason)

    def test_satisfied_initial_graph_is_preserved(self):
        material = SimpleNamespace(name='soft_effect', constants=(struct.pack('<I', 0x4d7ea234)+bytes(28),))
        binding = self.binding('effect_add_eyeoffset')
        self.assertEqual(bind_material_constants(material, binding), binding)

    def test_missing_constant_still_fails_without_compatible_sibling(self):
        material = SimpleNamespace(name='soft_effect', constants=())
        with self.assertRaisesRegex(ValueError, '0x4d7ea234'):
            bind_material_constants(material, self.binding('effect_zfeather_add'))
