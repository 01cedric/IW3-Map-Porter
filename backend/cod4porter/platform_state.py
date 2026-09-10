from __future__ import annotations
import json,hashlib
from collections import defaultdict
from pathlib import Path
from .material import MaterialRecord,MaterialStateConversion

_REF=Path(__file__).resolve().parent/'reference'/'platformstate_fix89b.json'
_REF_BYTES=_REF.read_bytes()
_REF_SHA256=hashlib.sha256(_REF_BYTES).hexdigest().upper()
_EXACT={k:bytes.fromhex(v) for k,v in json.loads(_REF_BYTES.decode('utf-8')).items()}
if len(_EXACT)!=94 or any(len(v)!=0x38 for v in _EXACT.values()):
    raise RuntimeError('Fix89B PlatformState evidence catalog corrupt')

_SIGNATURE_FIELD_COUNT=9
_STRUCTURAL_FIELDS=(0,2,3,4,5,6,7)
_RENDER_SHAPE_FIELDS=(2,3,4,5,6,7)

def _signature_parts(signature:str)->tuple[int,int,int,int,int,int,int,str,int]:
    parts=signature.split('|')
    if len(parts)!=_SIGNATURE_FIELD_COUNT:
        raise RuntimeError('Fix89B PlatformState signature has the wrong field count')
    try:
        return (
            int(parts[0]),int(parts[1]),int(parts[2]),int(parts[3]),
            int(parts[4]),int(parts[5]),int(parts[6]),parts[7],int(parts[8]),
        )
    except ValueError as exc:
        raise RuntimeError('Fix89B PlatformState signature contains a malformed field') from exc

def _project(parts:tuple,indexes:tuple[int,...])->tuple:
    return tuple(parts[index] for index in indexes)

_ROWS=tuple((signature,_signature_parts(signature),payload) for signature,payload in _EXACT.items())
_BY_STRUCTURAL=defaultdict(set)
_BY_RENDER_SHAPE=defaultdict(set)
_BY_SORT_KEY=defaultdict(set)
_STRUCTURAL_OBSERVATION_COUNT=defaultdict(int)
_RENDER_SHAPE_OBSERVATION_COUNT=defaultdict(int)
_SORT_KEY_OBSERVATION_COUNT=defaultdict(int)
for _signature,_parts,_payload in _ROWS:
    _structural=_project(_parts,_STRUCTURAL_FIELDS)
    _render_shape=_project(_parts,_RENDER_SHAPE_FIELDS)
    _BY_STRUCTURAL[_structural].add(_payload)
    _BY_RENDER_SHAPE[_render_shape].add(_payload)
    _BY_SORT_KEY[_parts[0]].add(_payload)
    _STRUCTURAL_OBSERVATION_COUNT[_structural]+=1
    _RENDER_SHAPE_OBSERVATION_COUNT[_render_shape]+=1
    _SORT_KEY_OBSERVATION_COUNT[_parts[0]]+=1
if any(len(payloads)!=1 for payloads in _BY_STRUCTURAL.values()):
    # Hash-table placement and surface/impact classification are not GPU-state inputs.  Even so,
    # fail closed if the hardware baseline ever disagrees after those two fields are projected out.
    raise RuntimeError('Fix89B PlatformState structural families conflict')

def build_signature(source:MaterialRecord,state:MaterialStateConversion)->str:
    if len(state.state_bits_entry)!=26:raise ValueError(f"Material '{source.name}' PS3 stateBitsEntry has {len(state.state_bits_entry)} bytes; expected 26")
    return '|'.join(map(str,(
        source.sort_key,source.hash_index,source.camera_region,source.state_flags,
        len(source.textures),len(source.constants),len(state.state_bits),state.state_bits_entry.hex().upper(),source.surface_type_bits
    )))

def try_resolve(source:MaterialRecord,state:MaterialStateConversion)->bytes|None:
    b=_EXACT.get(build_signature(source,state))
    return bytes(b) if b is not None else None

def resolve_exact(source:MaterialRecord,state:MaterialStateConversion)->bytes:
    sig=build_signature(source,state);b=_EXACT.get(sig)
    if b is None:
        raise ValueError(f"Material '{source.name}' has no unique Fix89B hardware PlatformState template for converted signature {sig}")
    return bytes(b)

def exact_signature_count()->int:return len(_EXACT)
def catalog_sha256()->str:return _REF_SHA256
def contains_signature(signature:str)->bool:return signature in _EXACT
def matches_exact(signature:str,payload:bytes)->bool:
    expected=_EXACT.get(signature)
    return expected is not None and bytes(payload)==expected


def resolution_tier(mode:str)->str:
    """Return the fidelity tier without promoting compatibility evidence to exact evidence."""
    if mode=='exact':return 'exact_signature'
    if mode in ('observed_structural_family','observed_render_state_family','observed_sortkey_family','observed_sortkey12_neutral_family'):
        return 'hardware_observed_family'
    if mode=='synthetic_neutral_runtime':return 'synthetic_assignment'
    raise ValueError(f'unknown PlatformState resolution mode {mode!r}')


def runtime_resolution_audit(source:MaterialRecord,state:MaterialStateConversion)->dict:
    """Describe why the runtime resolver selected its payload.

    ``hardware_observed_family`` means the bytes occur in the hardware-working Fix89B catalog,
    but the target signature itself was not observed.  It must therefore never be reported as an
    exact target match.  ``synthetic_assignment`` means even the target family was unobserved.
    """
    payload,mode=resolve_runtime_compatible(source,state)
    signature=build_signature(source,state)
    parts=_signature_parts(signature)
    structural=_project(parts,_STRUCTURAL_FIELDS)
    render_shape=_project(parts,_RENDER_SHAPE_FIELDS)
    if mode=='exact':
        evidence_scope='full_signature';observations=1
    elif mode=='observed_structural_family':
        evidence_scope='structural_signature_without_hash_or_surface';observations=_STRUCTURAL_OBSERVATION_COUNT[structural]
    elif mode=='observed_render_state_family':
        evidence_scope='render_state_signature_without_sort_hash_or_surface';observations=_RENDER_SHAPE_OBSERVATION_COUNT[render_shape]
    elif mode in ('observed_sortkey_family','observed_sortkey12_neutral_family'):
        evidence_scope='sort_key_family';observations=_SORT_KEY_OBSERVATION_COUNT[parts[0]]
    else:
        evidence_scope='none_for_target_family';observations=0
    return {
        'signature':signature,
        'mode':mode,
        'tier':resolution_tier(mode),
        'exact_signature_match':matches_exact(signature,payload),
        'payload_hardware_observed':payload in _EXACT.values(),
        'evidence_scope':evidence_scope,
        'catalog_observations':observations,
        'payload_sha256':hashlib.sha256(payload).hexdigest().upper(),
    }


def resolve_runtime_compatible(source:MaterialRecord,state:MaterialStateConversion)->tuple[bytes,str]:
    """Resolve a loader-safe PS3 PlatformState using only hardware-observed payload families when possible.

    The exact 94-signature catalog remains authoritative.  Compatibility inference is deliberately
    tiered.  First reuse a unique structural observation after projecting out only ``hashIndex``
    (hash-table placement) and ``surfaceTypeBits`` (surface/impact classification).  If that does
    not match, a sortKey may be reused only when every hardware observation for that key agrees.
    SortKey 12 is mixed, so only shapes outside the observed non-neutral 1/1/3 family may use its
    repeatedly observed neutral payload.  Completely unseen families use the globally observed
    neutral bytes as an explicitly synthetic assignment, never as an exact/family claim.
    """
    exact=try_resolve(source,state)
    if exact is not None:return exact,'exact'
    parts=_signature_parts(build_signature(source,state))
    structural_payloads=_BY_STRUCTURAL.get(_project(parts,_STRUCTURAL_FIELDS),set())
    if len(structural_payloads)==1:
        return bytes(next(iter(structural_payloads))),'observed_structural_family'
    # sortKey controls draw ordering and is serialized separately from the 0x38-byte payload.
    # A cross-sort projection is accepted only for an individually conflict-free family that
    # retains cameraRegion, stateFlags, texture/constant/state cardinality and the complete
    # converted stateBitsEntry. Ambiguous families (including the observed empty-state conflict)
    # remain excluded and fall through. This is hardware-family evidence, never an exact target.
    render_payloads=_BY_RENDER_SHAPE.get(_project(parts,_RENDER_SHAPE_FIELDS),set())
    if len(render_payloads)==1:
        return bytes(next(iter(render_payloads))),'observed_render_state_family'
    payloads=_BY_SORT_KEY.get(source.sort_key,set())
    if payloads and len(payloads)==1:
        return bytes(next(iter(payloads))),'observed_sortkey_family'
    if source.sort_key==12:
        # Two non-neutral observations share cam=0, texture=1, constant=1, stateCount=3.
        # Do not infer that family without an exact signature. All other observed SortKey-12
        # structural families use the same neutral payload.
        if not (source.camera_region==0 and len(source.textures)==1 and len(source.constants)==1 and len(state.state_bits)==3):
            neutral={
                payload for _,row,payload in _ROWS
                if row[0]==12 and not (row[2]==0 and row[4]==1 and row[5]==1 and row[6]==3)
            }
            if len(neutral)==1:return bytes(next(iter(neutral))),'observed_sortkey12_neutral_family'
    # Last-resort runtime candidate: zero is not fabricated from thin air; it is the dominant
    # hardware-observed Fix89B PlatformState payload. It is still NOT an exact signature match.
    neutral=bytes(0x38)
    if neutral not in _EXACT.values():raise RuntimeError('neutral PlatformState lost hardware evidence')
    return neutral,'synthetic_neutral_runtime'
