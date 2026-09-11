"""Retail PS3 TechniqueSet donor catalog: load and adapt for transplantation.

The emulator-side ``techsetcheck.py`` extracts complete techsets from real
linked PS3 zones into a manifest + payload file.  This module turns those
records into the same :class:`CompiledTechniqueSet` shape the compiled path
produces, so one serializer emits both.  Donor payloads are byte-faithful:
shader Cg binaries, root control bytes and vertex declarations are written
exactly as the console shipped them.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .techset_compile import (
    CompiledArg, CompiledPass, CompiledTechnique, CompiledTechniqueSet,
    PS3_SLOT_TO_PC, PS3_TECHNIQUE_SLOTS,
)

ARG_LITERALS = (1, 7)


class DonorCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class DonorShader:
    kind: str
    name: str
    blob: bytes
    content_key: str
    root_control: bytes | None
    translation: object = None
    parameter_index: dict | None = None

    @property
    def control_bytes(self) -> bytes:
        if self.root_control is not None:
            return self.root_control
        from . import cgbinary
        return cgbinary.fragment_control_bytes(self.blob)


@dataclass(frozen=True)
class DonorDecl:
    raw: bytes
    content_key: str
    stream_count: int
    has_optional_source: bool

    def serialized(self) -> bytes:
        return self.raw


class DonorCatalog:
    def __init__(self, manifest: dict, payloads: bytes, source: str):
        self.manifest = manifest
        self.payloads = payloads
        self.source = source
        self.by_name = {row['name'].casefold(): row for row in manifest.get('techsets', ())}

    def __contains__(self, name: str) -> bool:
        return name.casefold() in self.by_name

    def names(self):
        return sorted(self.by_name)

    def techniqueset(self, name: str, *, source_asset_index: int = -1) -> CompiledTechniqueSet:
        row = self.by_name.get(name.casefold())
        if row is None:
            raise DonorCatalogError(f'donor catalog has no techset {name!r}')
        return _adapt(row, self.payloads, source_asset_index)


def load_donor_catalog(json_path) -> DonorCatalog:
    path = Path(json_path)
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('schema') != 'iw3-ps3-techset-donor/v1':
        raise DonorCatalogError(f'{path} is not a techset donor catalog')
    payload_file = path.with_name(str(manifest.get('payload_file', '')))
    if not payload_file.is_file():
        raise DonorCatalogError(f'donor payload file missing: {payload_file}')
    return DonorCatalog(manifest, payload_file.read_bytes(), str(path))


def _payload(window: dict, payloads: bytes) -> bytes:
    offset, size = int(window['offset']), int(window['size'])
    blob = payloads[offset:offset + size]
    if len(blob) != size:
        raise DonorCatalogError('donor payload window outside payload file')
    digest = hashlib.sha256(blob).hexdigest()
    if digest != window.get('sha256'):
        raise DonorCatalogError('donor payload digest mismatch')
    return blob


def _adapt(row: dict, payloads: bytes, source_asset_index: int) -> CompiledTechniqueSet:
    shaders = {}
    for key, record in row.get('shaders', {}).items():
        blob = _payload(record['payload'], payloads)
        control = bytes.fromhex(record['root_control']) if record.get('root_control') else None
        shaders[key] = DonorShader(
            record['kind'], record['name'], blob,
            f"donor:{record['kind'][0]}:{record['payload']['sha256'][:12]}",
            control)
    technique_cache: dict[str, CompiledTechnique] = {}
    slots: list[CompiledTechnique | None] = [None] * PS3_TECHNIQUE_SLOTS
    for slot_text, technique_row in row.get('slots', {}).items():
        slot = int(slot_text)
        if not 0 <= slot < PS3_TECHNIQUE_SLOTS:
            raise DonorCatalogError(f'donor slot {slot} outside the PS3 domain')
        digest_material = json.dumps(technique_row, sort_keys=True, default=str)
        cache_key = technique_row['name'] + ':' + hashlib.sha256(digest_material.encode()).hexdigest()[:12]
        technique = technique_cache.get(cache_key)
        if technique is None:
            passes = []
            for pass_row in technique_row['passes']:
                decl = None
                if pass_row.get('decl_raw'):
                    raw = bytes.fromhex(pass_row['decl_raw'])
                    decl = DonorDecl(raw, 'donor:' + hashlib.sha256(raw).hexdigest()[:12],
                                     raw[0], bool(raw[1]))
                vertex = shaders.get(pass_row['vertex_shader'])
                pixel = shaders.get(pass_row['pixel_shader'])
                if vertex is None or pixel is None:
                    raise DonorCatalogError(
                        f"donor technique {technique_row['name']!r} references a missing shader")
                args = []
                for argument in pass_row['args']:
                    literal = tuple(argument['literal']) if argument.get('literal') else None
                    args.append(CompiledArg(int(argument['type']), int(argument['dest']),
                                            int(argument['value']) & 0xFFFFFFFF, literal))
                counts = pass_row['counts']
                passes.append(CompiledPass(decl, vertex, pixel, counts[0], counts[1],
                                           counts[2], int(pass_row['custom_sampler_flags']),
                                           tuple(args), ()))
            # The whole manifest row (flags, counts, decl, args, shader keys)
            # is the identity: two donor techniques may only merge into one
            # emitted technique when every serialized field agrees.
            technique = CompiledTechnique(
                technique_row['name'], int(technique_row['flags']), tuple(passes),
                PS3_SLOT_TO_PC[slot],
                'donor:' + hashlib.sha256(digest_material.encode()).hexdigest()[:12])
            technique_cache[cache_key] = technique
        slots[slot] = technique
    return CompiledTechniqueSet(
        row['name'], int(row['world_vertex_format']), tuple(slots), (),
        (f"transplanted from retail PS3 data ({row.get('source', 'donor catalog')})",),
        source_asset_index)
