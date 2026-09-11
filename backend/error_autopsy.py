"""Structured self-diagnosis for conversion failures.

When a job dies, the bridge writes an ``error-autopsy.json`` next to the other
artifacts: the exception, the porter-side stack frames, the map/version
context, and - when the error text matches a KNOWN class - what the failure
means and which knob (if any) addresses it.  Known DEGRADABLE classes should
never reach this file (the porter quarantines or omits them per asset);
seeing one here means a policy disabled that path or a new variant appeared.

This is deliberately a catalog, not a guesser: unknown errors are recorded
verbatim with full context and stay hard failures.  Inventing a "fix" for an
unproven failure is how silent PS3 freezes are made.
"""
from __future__ import annotations

import re
import traceback

SCHEMA = 'iw3-error-autopsy/v1'

# (pattern, classification, meaning, action)
KNOWN_CLASSES = (
    (r'No space left on|\[Errno 28\]',
     'environment/disk-full', 'A drive ran out of space while writing.',
     'Free space on the output drive and the Windows TEMP drive, then run the job again.'),
    (r'cannot safely bind (its owned )?PS3 TechniqueSet',
     'material/constant-contract', 'A material does not satisfy the constant/sampler tables its PS3 shader graph scans for.',
     'Runtime-compatible ports quarantine this material to $default automatically; a full port stops by design.'),
    (r'has no native PS3 binding|Unsupported PS3 shaders',
     'techset/unsupported', 'A TechniqueSet could not be sourced natively, from a donor, or by compilation.',
     'Check shader_compilation=auto and the port report; the FX omission policy handles effect-only users.'),
    (r'missing constants|missing texture samplers',
     'material/contract-detail', 'The exact missing nameHashes are listed in the message.',
     'Runtime-compatible ports quarantine the material; otherwise fix the source material.'),
    (r'fewer than two backward topology proofs|walk drift|brush (edge|side) (range|pointer)',
     'parser/evidence-model', 'A source-zone layout did not fit the proof model for this asset family.',
     'This is porter-side model work, not map damage: report the map name and this file so the model can be extended.'),
    (r'relocation target .* was never defined',
     'writer/symbol-closure', 'The zone writer referenced a symbol no node defined in this run.',
     'Porter-side defect: report the map name and this file.'),
    (r'DB_AllocXZoneMemory|zone budget|cannot be brought under',
     'budget/zone-size', 'The converted map does not fit the measured PS3 zone budget.',
     'Reduce the source map, lower --image-budget-min-dimension, or supply measured retail evidence for a higher budget.'),
    (r'support zones|no target support zones',
     'checks/support-zones', 'Native-name resolution was skipped because no retail support zones are configured.',
     'Configure code_post_gfx_mp / ui_mp / common_mp in Emulator & Linker to run the native checks.'),
    (r'FX runner|unresolved packed visual table',
     'fx/reference-evidence', 'An effect\'s packed references could not be proven from source topology.',
     'The omission policy removes exactly that effect; policy error stops instead.'),
    (r'IWI (dimensions|mip count) do not match|resolved IWI canonical name',
     'loadscreen/source-mismatch', 'The loading-screen IWI does not match the PC load metadata.',
     'Check the IWD for the correct loadscreen image; format-only differences are accepted automatically.'),
    (r'expected one ClipMap root|expected one plane array|ClipMap .* cardinality',
     'parser/structure-location', 'A world structure could not be located uniquely in the source zone.',
     'Porter-side model work: report the map name and this file.'),
    (r'access to unmapped address|MEMORY FAULT after \d+ assets',
     'linker/pointer-cell-fault', 'The emulated console loader dereferenced a pointer value the zone cannot define.',
     'Porter-side defect: the link report (zone.bin.link.json) carries fault forensics naming the '
     'failing asset and the file offsets of the poisoned pointer cells - report the map name and that file.'),
)


def classify(message: str) -> dict:
    for pattern, label, meaning, action in KNOWN_CLASSES:
        if re.search(pattern, message, re.IGNORECASE):
            return {'class': label, 'meaning': meaning, 'action': action, 'known': True}
    return {'class': 'unknown', 'known': False,
            'meaning': 'No catalog entry matches this failure.',
            'action': 'Report the map name and this file; unknown failures stay hard '
                      'stops by design instead of guessing.'}


def build_autopsy(exc: BaseException, *, command: str = '', version: str = '',
                  map_name: str = '', stage: str = '') -> dict:
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__ or None):
        path = frame.filename.replace('\\', '/')
        if 'cod4porter' in path or 'backend' in path:
            frames.append({'file': path.rsplit('/backend/', 1)[-1],
                           'line': frame.lineno, 'function': frame.name,
                           'code': (frame.line or '')[:160]})
    message = f'{type(exc).__name__}: {exc}'
    return {
        'schema': SCHEMA,
        'version': version, 'command': command, 'map': map_name,
        'stage': stage,
        'error': message,
        'classification': classify(str(exc)),
        'porter_frames': frames[-12:],
    }
