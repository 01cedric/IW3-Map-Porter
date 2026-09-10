from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from .v4_proofs import Allocation, PackedRequest, replay_allocations, solve_uniform_basis, texturedef_signature

IMAGE_ALIAS_KIND='gfximage_root_alias'
LOADDEF_ALIAS_KIND='gfximage_loaddef_member_alias'


def resolve_packed_images(
    inline_texturedefs:Sequence[dict],
    packed_texturedefs:Sequence[dict],
    allocation_rows:Sequence[dict],
    phases=(0,1,2,3),
    minimum_constraints:int=2,
    proven_base:int|None=None,
    proven_phase:int|None=None,
)->dict:
    """Two-stage fail-closed packed MaterialTextureDef::image identity proof.

    Stage 1: exact complete TextureDef signature (hash/start/end/sampler/semantic) may bind only
    when every inline observation with that signature names the same image.

    Stage 2: unresolved packed image pointers must address a GfxImage TEMP-XAsset root alias under
    one common block-4 allocation basis.  GfxImageLoadDef reusable-member aliases are a distinct
    kind and can never satisfy an image-root request.
    """
    sigmap=defaultdict(set)
    for row in inline_texturedefs:
        sigmap[texturedef_signature(row)].add(str(row['image_identity']))

    resolved=[];unresolved=[];blockers=[]
    for row in packed_texturedefs:
        sig=texturedef_signature(row);ids=sigmap.get(sig,set())
        desc=str(row.get('description') or f'packed_image_{len(resolved)+len(unresolved)}')
        if len(ids)==1:
            resolved.append({'description':desc,'identity':next(iter(ids)),'mode':'exact_texturedef_signature','signature':sig})
        elif len(ids)>1:
            blockers.append({'description':desc,'reason':'TextureDef signature maps to multiple inline image identities','candidates':sorted(ids),'signature':sig})
        else:
            if 'packed_offset' not in row:
                blockers.append({'description':desc,'reason':'no signature match and no packed block-4 offset','signature':sig})
            else:
                unresolved.append((desc,row,sig))

    if blockers:
        return {'passed':False,'bindings':resolved,'blockers':blockers,'stage2':None}
    if not unresolved:
        return {'passed':True,'bindings':resolved,'blockers':[],'stage2':{'passed':True,'mode':'not_needed'}}

    alloc=[]
    for row in allocation_rows:
        kind=str(row['kind'])
        if kind not in (IMAGE_ALIAS_KIND,LOADDEF_ALIAS_KIND):
            raise ValueError(f'unknown image allocation kind {kind}')
        alloc.append(Allocation(str(row['identity']),kind,int(row['size']),int(row.get('alignment',4)),True))

    requests=[PackedRequest(desc,int(row['packed_offset']),IMAGE_ALIAS_KIND) for desc,row,_ in unresolved]
    if proven_base is None:
        proof=solve_uniform_basis(alloc,requests,phases=phases,minimum_constraints=minimum_constraints)
    else:
        if proven_phase is None: raise ValueError('proven_phase is required with proven_base')
        replay=replay_allocations(alloc,proven_phase)
        roots={proven_base+a.relative_offset:a for a in replay if a.kind==IMAGE_ALIAS_KIND}
        bindings=[];stage_block=[]
        for req in requests:
            a=roots.get(req.logical_offset)
            if a is None:stage_block.append({'description':req.description,'logical_offset':req.logical_offset,'reason':'anchored basis does not land on GfxImage root alias'})
            else:bindings.append({'description':req.description,'logical_offset':req.logical_offset,'identity':a.identity,'kind':a.kind,'relative_offset':a.relative_offset})
        proof={'passed':not stage_block,'base':proven_base,'phases':[proven_phase],'bindings':bindings,'blockers':stage_block,'anchored':True}

    if not proof.get('passed'):
        return {'passed':False,'bindings':resolved,'blockers':[{'reason':'packed GfxImage root-alias proof failed','proof':proof}],'stage2':proof}

    by_desc={b['description']:b for b in proof['bindings']}
    for desc,row,sig in unresolved:
        b=by_desc.get(desc)
        if not b:
            blockers.append({'description':desc,'reason':'basis proof omitted request'})
            continue
        resolved.append({'description':desc,'identity':b['identity'],'mode':'temp_xasset_root_alias','packed_offset':int(row['packed_offset']),'signature':sig})
    identities_by_desc=defaultdict(set)
    for b in resolved:identities_by_desc[b['description']].add(b['identity'])
    dup=[{'description':d,'candidates':sorted(ids)} for d,ids in identities_by_desc.items() if len(ids)!=1]
    blockers.extend(dup)
    return {'passed':not blockers and len(resolved)==len(packed_texturedefs),'bindings':resolved,'blockers':blockers,'stage2':proof}
