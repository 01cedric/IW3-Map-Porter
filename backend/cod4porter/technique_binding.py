from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import json,re
from pathlib import Path
from typing import Iterable
from .pc_technique import TechniqueSet

_REF=Path(__file__).resolve().parent/'reference'/'retail_techniques.json'
_AUDITED={x.casefold():x for x in json.loads(_REF.read_text(encoding='utf-8'))}

# Availability catalog measured from a real PS3 installation (2026-09-03): every name is
# either owned by an installed support zone (common_mp / code_post_gfx_mp / ui_mp) or
# referenced by a shipping Retail PS3 map zone (mp_shipment / mp_crash).  A PS3 map may
# only reference a TechniqueSet that exists there - the porter never converts a shader
# graph, so a name outside this catalog cannot resolve through DB_FindXAssetHeader.
_AVAIL_REF=Path(__file__).resolve().parent/'reference'/'ps3_technique_availability.json'


def _source_resolves(source:str)->bool:
    """Does this piece of provenance prove the console can resolve the name?

    Measured 2026-09-04.  A PS3 MP map runs with ``common_mp.ff``,
    ``code_post_gfx_mp.ff``, ``code_pre_gfx_mp.ff`` and ``ui_mp.ff`` loaded - and with
    NO other map zone.  Retail ``mp_crash.ff`` owns 98 MaterialTechniqueSets and
    ``mp_shipment.ff`` owns 69: on PS3 a map carries its own world shaders.  A name that
    was only ever seen *owned by another map zone* therefore cannot be referenced from
    this map; ``DB_FindXAssetHeader`` has nothing to find, and that is a load failure,
    not a cosmetic issue.  Treating those names as available is what put 22 dead
    TechniqueSet references into the generated zone.

    Evidence that does resolve:
      ``support:<zone>:owned``       the always-loaded zone contains the asset
      ``support:<zone>:referenced``  an always-loaded zone resolves it, so it exists
      ``retail_map:<zone>:referenced`` a shipping map resolves it on this installation
    """
    if source.startswith('support:'):
        return source.endswith(':owned') or source.endswith(':referenced')
    if source.startswith('retail_map:'):
        return source.endswith(':referenced')
    return False


def _load_availability()->tuple[dict[str,tuple[tuple[str,...],tuple[str,...]]],dict[str,tuple[str,...]]]:
    # A zone may spell the same TechniqueSet in more than one case ("2d" and "2D").
    # Keep every spelling: IW3 resolves the reference by exact string, so the one the
    # PC source already uses is preferred and the lowest-sorted spelling is the fallback.
    out:dict[str,tuple[list[str],list[str]]]={}
    for entry in json.loads(_AVAIL_REF.read_text(encoding='utf-8'))['entries']:
        name=str(entry['name']);key=name.casefold()
        spellings,sources=out.setdefault(key,([],[]))
        if name not in spellings:spellings.append(name)
        for source in entry.get('sources',()):
            if source not in sources:sources.append(source)
    resolvable={};unresolvable={}
    for key,(spellings,sources) in out.items():
        proof=tuple(x for x in sources if _source_resolves(x))
        if proof:resolvable[key]=(tuple(sorted(spellings)),proof)
        else:unresolvable[key]=tuple(sources)
    return resolvable,unresolvable

_AVAILABILITY,_NOT_RESOLVABLE=_load_availability()


def _resolve(candidate:str)->tuple[str,tuple[str,...]]|None:
    entry=_AVAILABILITY.get(candidate.casefold())
    if entry is None:return None
    spellings,sources=entry
    return (candidate if candidate in spellings else spellings[0]),sources

class TechniqueEvidence(str,Enum):
    AUDITED_RETAIL='audited_retail'
    PS3_AVAILABLE_EXACT='ps3_available_exact'
    PS3_FAMILY_SUBSTITUTION='ps3_available_family_substitution'
    PC_SHARED='pc_shared_support_reference'
    CANONICAL_STOCK='canonical_iw3_stock_family'
    COMPILED_RSX='compiled_pc_rsx_translation'
    RETAIL_DONOR='ps3_retail_donor_transplant'
    UNSUPPORTED='unsupported_custom'

@dataclass(frozen=True)
class TechniqueBinding:
    source_name:str
    candidate_name:str
    source_was_shared:bool
    evidence:TechniqueEvidence
    can_bind_native:bool
    reason:str
    source_asset_index:int=-1
    substitution:str=''
    # COMPILED_RSX: the CompiledTechniqueSet artifact the zone will own.
    # RETAIL_DONOR: the donor payload record extracted from a real PS3 zone.
    compiled:object=None

    @property
    def owns_ps3_techset(self)->bool:
        return self.evidence in (TechniqueEvidence.COMPILED_RSX,TechniqueEvidence.RETAIL_DONOR) and self.compiled is not None

_EFFECT=re.compile(r'^(?:(?:m|w)c?_)?effect(?:_(?:zfeather|falloff|add|nofog|eyeoffset))*$',re.I)
_DIST=re.compile(r'^(?:wc?_)?distortion_scale(?:_zfeather)?$',re.I)
_PARTICLE=re.compile(r'^particle_cloud(?:_outdoor)?(?:_add)?$',re.I)
_SIMPLE=re.compile(r'^(?:m|mc|w|wc)_(?:ambient_t0c0|shadowcaster|threed_cinematic|sky|water)$',re.I)
# Stock CoD4 PC ships more unlit modifiers than Version 15 modelled: mp_crash alone
# owns mc_unlit_alphatest, wc_unlit_distfalloff and wc_unlit_falloff_add, and rejecting
# them made the TechniqueSet scanner miscount 216 against 226 XAssets - the porter
# refused a stock map outright.
_UNLIT=re.compile(r'^(?:m|mc|w|wc)_unlit(?:_(?:add|multiply|replace|alphatest|distfalloff|falloff))*$',re.I)
# Custom maps use more lighting qualifiers than the two stock maps do: mp_bo2raid3
# owns mc_l_sm_scroll_* and mc_l_hsm_scroll_*, and rejecting them made the scanner
# count 188 against 196 TechniqueSet XAssets.
_LIGHTING=re.compile(r'^(?:m|mc|w|wc)_l_(?:(?:hsm|sm)_)?(?:flag_|skin_|scroll_)?[abrt]0c0(?:d0)?(?:n0)?(?:s0)?$',re.I)

def canonical_stock_name(name:str)->bool:
    if not name:return False
    if name.casefold() in {'2d','cinematic','d_cinematic','wc_default','w_default','mc_default','m_default'}:return True
    return any(r.fullmatch(name) for r in (_EFFECT,_DIST,_PARTICLE,_SIMPLE,_UNLIT,_LIGHTING))


def _drop_detail(name:str)->str:
    """Remove the detail-map channel from an IW3 lighting technique name."""
    return re.sub(r'(?<=c0)d0','',name)


# Havana owns sm2/mc_l_sm_a0c0, sm2/mc_l_hsm_a0c0 and their SM3 twins.
# Their complete roots and inline shader payloads verify a0 as a source family.
# Accepting it does not declare a native equivalent: availability and the
# material-constant contract still gate the separately reported base-family
# substitution.  (Sampler tables are gated only for owned compiled/donor
# techsets, whose argument tables ship with the artifact; the retail contract
# catalog records constants only.)
# An IW3 lighting technique ends in a fixed channel order: <a|b|r|t>0c0 [d0] [n0] [s0].
_CHANNELS=re.compile(r'^(?P<head>.*[abrt]0c0)(?P<d>d0)?(?P<n>n0)?(?P<s>s0)?$')


def _add_channels(name:str)->list[str]:
    """Offer the same lighting model with an extra channel enabled.

    PS3 frequently ships only the fully-featured member of a family - ``mc_l_sm_t0c0s0``
    exists where ``mc_l_sm_t0c0`` does not.  A channel is inserted at its canonical
    position, never appended, so ``..._r0c0s0`` upgrades to ``..._r0c0n0s0``.
    """
    m=_CHANNELS.match(name)
    if m is None:return []
    head=m.group('head');d=m.group('d') or '';n=m.group('n') or '';sp=m.group('s') or ''
    out=[]
    if not sp:out.append(head+d+n+'s0')
    if not n:out.append(head+d+'n0'+sp)
    if not n and not sp:out.append(head+d+'n0s0')
    return out


def _drop_channels(name:str)->list[str]:
    """Offer the same lighting model with a texture channel removed.

    The mirror image of ``_add_channels``, and strictly lower fidelity, so it is only
    reached once adding a channel has failed.  PS3 ships ``mc_l_sm_t0c0s0`` but not
    ``mc_l_sm_t0c0n0s0``: dropping ``n0`` keeps the lighting model and the colour map
    and only gives up normal-map sampling.
    """
    m=_CHANNELS.match(name)
    if m is None:return []
    head=m.group('head');d=m.group('d') or '';n=m.group('n') or '';sp=m.group('s') or ''
    out=[]
    if n:out.append(head+d+sp)
    if sp:out.append(head+d+n)
    if n and sp:out.append(head+d)
    if d:out.append(head+n+sp)
    return [x for x in out if x!=name]


_BASE_LETTER=re.compile(r'(?P<head>^(?:(?:m|mc|w|wc)_)?l_(?:(?:hsm|sm)_)?(?:flag_|skin_|scroll_)?)(?P<base>[abrt])0c0')


def _base_variants(name:str)->list[str]:
    """Offer the same lighting family with a different base-map letter.

    ``b0`` / ``r0`` / ``t0`` select how the surface takes its diffuse lighting.  The
    always-loaded PS3 zones ship the world family only as ``wc_l_sm_b0c0*`` while PC
    world materials are written ``w_l_sm_r0c0*``, so without this step every world
    surface of a ported map has no resolvable TechniqueSet at all.  It is the least
    faithful transformation in the ladder and therefore the last one offered.
    """
    m=_BASE_LETTER.match(name)
    if m is None:return []
    return [name[:m.start('base')]+letter+name[m.end('base'):]
            for letter in ('b','r','t') if letter!=m.group('base')]


# Families whose PS3 spelling drops the vertex-format prefix entirely.
_PREFIXED_SIMPLE=re.compile(r'^(?:m|mc|w|wc)_(?P<tail>shadowcaster|sky|water|unlit(?:_(?:add|multiply|replace|alphatest|distfalloff|falloff))*)$',re.I)


def _family_fallbacks(name:str)->list[str]:
    """Non-lighting families: PS3 spellings and the documented degradations.

    * ``m_shadowcaster`` -> ``shadowcaster`` - the depth-only pass is one shared graph.
    * ``X_unlit_add`` -> ``X_unlit`` -> ``unlit`` - the blend modifier is a state, and
      the unprefixed ``unlit`` is what the always-loaded zones actually ship.
    * ``X_ambient_t0c0`` -> ``X_l_sm_t0c0`` - ambient-only lighting is the shadow-mapped
      graph with no light contribution; the lit graph is the closest that exists.
    * ``X_sky`` -> ``unlit`` - the sky graph is map-owned on PS3; unlit draws the same
      texture without the sky-specific fog term.
    * ``X_water`` -> ``X_l_sm_b0c0`` - likewise map-owned; the surface then renders as a
      static lit texture instead of an animated one.
    """
    out:list[str]=[]
    m=_PREFIXED_SIMPLE.match(name)
    if m is not None:
        tail=m.group('tail')
        out.append(tail)
        if tail.startswith('unlit'):
            out.append(name[:m.start('tail')]+'unlit');out.append('unlit')
    if name.endswith('_unlit_add') or name.endswith('_unlit_multiply') or name.endswith('_unlit_replace'):
        out.append(name.rsplit('_',1)[0])
    prefix=''
    for candidate in ('mc_','wc_','m_','w_'):
        if name.startswith(candidate):prefix=candidate;break
    tail=name[len(prefix):]
    if tail=='ambient_t0c0':
        out.append(prefix+'l_sm_t0c0')
    if tail=='sky':
        out.extend(('unlit','m_unlit'))
    if tail=='default':
        # PC diagnostic/default shader family. Preserve model/world layout; the
        # corresponding PS3 basic lit family is an explicit fidelity fallback.
        out.append((prefix or 'wc_')+'l_sm_b0c0')
    if tail=='water':
        out.append((prefix or 'wc_')+'l_sm_b0c0')
    return [x for x in out if x!=name]



_QUALIFIER=re.compile(r'^(?P<head>(?:(?:m|mc|w|wc)_)?l_(?:(?:hsm|sm)_)?)(?P<qual>flag_|skin_|scroll_)(?P<tail>[abrt]0c0.*)$')


def _drop_lighting_qualifier(name:str)->list[str]:
    """Offer the same lighting model without its flag/skin/scroll qualifier.

    ``scroll_`` animates the texture coordinates and ``flag_``/``skin_`` add vertex
    animation; none of the three exists on PS3 outside ``mc_l_sm_flag_t0c0n0s0`` and
    ``m_l_sm_skin_r0c0n0s0``.  mp_bo2raid3 owns four ``scroll_`` sets whose family has no
    PS3 member at all, so without this step the ladder can never leave it.  Dropping the
    qualifier keeps every texture channel and only gives up the animation.
    """
    m=_QUALIFIER.match(name)
    return [m.group('head')+m.group('tail')] if m else []


def ps3_candidates(name:str)->tuple[str,...]:
    """PC -> PS3 TechniqueSet name candidates, most faithful first.

    Every transformation is a documented property of the IW3 technique-name grammar,
    and each one is only ever *offered*: a candidate is used solely when the measured
    PS3 availability catalog actually contains it.

    ``hsm`` -> ``sm``
        The hard-shadow-map lighting variant does not ship on PS3.  Neither Retail PS3
        map references a single ``_hsm_`` technique; both use ``_sm_`` throughout.
    ``mc_`` <-> ``m_`` and ``wc_`` <-> ``w_``
        The ``c`` forms carry the vertex-colour channel.  Retail PS3 maps reference both
        spellings of the same lighting model, so the sibling is the closest available
        graph when one of the two is absent.  ``m`` (model) and ``w`` (world) are
        different vertex formats and are never exchanged.
    drop ``d0``
        Removes the detail-map channel, which several PS3 lighting graphs omit.
    """
    tiers:list[list[str]]=[[name]]
    def expand(previous:list[str])->list[str]:
        out:list[str]=[]
        for base in previous:
            # Fidelity order, most faithful first.  Everything above _drop_detail keeps
            # every texture channel the PC material declared.
            if '_hsm_' in base:out.append(base.replace('_hsm_','_sm_'))
            # Every shipping PS3 lighting graph carries the soft-shadow qualifier; a PC
            # name without one is the same model minus shadow-map sampling.
            if '_l_' in base and '_l_sm_' not in base and '_l_hsm_' not in base:
                out.append(base.replace('_l_','_l_sm_',1))
            # PS3 often ships only the fully-featured member of a lighting family
            # (mc_l_sm_t0c0s0 exists where mc_l_sm_t0c0 does not).  Adding a channel
            # qualifier keeps the whole material and only enables a sampler it may not
            # feed, so it outranks giving up the vertex-colour channel or the detail map.
            out.extend(_add_channels(base))
            if base.startswith('mc_'):out.append('m_'+base[3:])
            elif base.startswith('m_'):out.append('mc_'+base[2:])
            if base.startswith('wc_'):out.append('w_'+base[3:])
            elif base.startswith('w_'):out.append('wc_'+base[2:])
            stripped=_drop_detail(base)
            if stripped!=base:out.append(stripped)
            out.extend(_effect_variants(base))
            out.extend(_family_fallbacks(base))
            out.extend(_drop_lighting_qualifier(base))
            # Losing a channel or changing the base-map letter both give up something the
            # PC material declared, so they come after every lossless transformation.
            out.extend(_drop_channels(base))
            out.extend(_base_variants(base))
        return out
    # Insertion order inside a tier is the fidelity order set by ``expand``:
    # hsm->sm keeps every texture channel and only softens the shadow, so it is
    # preferred over dropping the vertex-colour channel or the detail map.
    seen={name}
    frontier=[name]
    for _ in range(5):
        nxt=[]
        for x in expand(frontier):
            if x not in seen:
                seen.add(x);nxt.append(x)
        if not nxt:break
        tiers.append(nxt);frontier=nxt
    return tuple(x for tier in tiers for x in tier)


def _effect_variants(name:str)->list[str]:
    """Offer the effect/distortion/cinematic families with their modifiers toggled.

    ``_zfeather`` (soft depth intersection) and ``_nofog`` are independent modifiers on
    the same graph, and PS3 ships a different subset than PC: it has
    ``distortion_scale_zfeather`` but no plain ``distortion_scale``, and
    ``effect_add_nofog`` but no plain ``effect_add``.  These families are also written
    unprefixed on PS3, so the ``m_``/``mc_``/``w_``/``wc_`` prefix is droppable here -
    unlike in the lighting families, where the prefix selects the vertex format.
    """
    out:list[str]=[]
    if name.endswith('threed_cinematic'):
        out.append('cinematic')
    if 'effect' not in name and 'distortion_scale' not in name:
        return out
    for prefix in ('mc_','wc_','m_','w_'):
        if name.startswith(prefix):
            out.append(name[len(prefix):]);break
    if '_nofog' not in name:
        out.append(name+'_nofog')
    if '_zfeather' not in name:
        if 'distortion_scale' in name:
            out.append(name.replace('distortion_scale','distortion_scale_zfeather',1))
        else:
            base,sep,rest=name.partition('effect')
            if sep:out.append(base+'effect_zfeather'+rest)
    if name.endswith('_eyeoffset'):
        # The eye-offset variant only changes vertex depth bias; PS3 ships the family
        # without it far more often than with it.
        out.append(name[:-len('_eyeoffset')])
    if '_add' not in name and 'effect' in name:
        # PS3 ships several effect graphs only in their additive form
        # (effect_zfeather_falloff_add exists, effect_zfeather_falloff does not).
        out.append(name+'_add')
    return out


def bind_technique(t:TechniqueSet)->TechniqueBinding:
    candidate=t.ps3_reference_candidate
    available=_resolve(candidate)
    if available is not None:
        retail=_AUDITED.get(candidate.casefold())
        return TechniqueBinding(t.name,available[0],t.shared,
            TechniqueEvidence.AUDITED_RETAIL if retail is not None else TechniqueEvidence.PS3_AVAILABLE_EXACT,
            True,
            'Name is present in the measured PS3 availability catalog ('+', '.join(available[1])+').',
            t.source_asset_index)
    for alternative in ps3_candidates(candidate)[1:]:
        available=_resolve(alternative)
        if available is None:continue
        return TechniqueBinding(t.name,available[0],t.shared,TechniqueEvidence.PS3_FAMILY_SUBSTITUTION,True,
            f'PC TechniqueSet {candidate!r} does not exist on PS3; substituted the closest available '
            f'graph {available[0]!r} ('+', '.join(available[1])+').',
            t.source_asset_index,substitution=f'{candidate} -> {available[0]}')
    return TechniqueBinding(t.name,candidate,t.shared,TechniqueEvidence.UNSUPPORTED,False,
        'No PS3 TechniqueSet with this name or a documented sibling exists in the measured '
        'availability catalog; the console cannot resolve the reference.',t.source_asset_index)

def bind_all(items:Iterable[TechniqueSet])->tuple[TechniqueBinding,...]:
    out=tuple(bind_technique(x) for x in items)
    bad=[x for x in out if not x.can_bind_native]
    if bad:
        names=sorted({x.candidate_name for x in bad},key=str.casefold)
        raise ValueError(
            f'{len(names)} PC TechniqueSet name(s) have no PS3 equivalent in the measured '
            'availability catalog (cod4porter/reference/ps3_technique_availability.json): '
            +', '.join(names[:30])+('...' if len(names)>30 else '')
            +'. Parsing succeeded. Add a PS3 support zone only if it actually contains a matching '
            'technique, or let the PC-to-RSX shader compiler own the TechniqueSet (shader_compilation=auto); '
            'this call binds names only and performs no compilation.'
        )
    return out

def audited_retail_names()->frozenset[str]:
    return frozenset(_AUDITED.values())

def available_ps3_names()->frozenset[str]:
    return frozenset(name for spellings,_ in _AVAILABILITY.values() for name in spellings)

def is_available(name:str)->bool:
    return name.casefold() in _AVAILABILITY

def availability_sources(name:str)->tuple[str,...]:
    entry=_AVAILABILITY.get(name.casefold())
    return entry[1] if entry else ()
