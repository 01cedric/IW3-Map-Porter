"""Omit FX whose structural dependency graph reaches an unsupported PS3 shader."""
from collections import defaultdict
from dataclasses import dataclass,field


def norm(name):return (name or '').replace('\\','/').lstrip(',').casefold()

@dataclass
class FxOmission:
    roots:set=field(default_factory=set)
    asset_indices:set=field(default_factory=set)
    effect_names:set=field(default_factory=set)
    image_names:set=field(default_factory=set)
    report:dict=field(default_factory=dict)


def plan_omissions(index, techniques, bindings, policy='omit', *, unresolvable_fx=None):
    """``unresolvable_fx`` maps FxEffectDef root offsets to a reason string:
    effects whose packed runner/impact references could not be proven from
    source topology.  They seed the same transitive omission as unsupported
    shaders, so one odd effect costs that effect (and its exclusive
    dependents), never the whole conversion."""
    if policy not in ('omit','error'):raise ValueError('Unsupported FX policy must be omit or error')
    unresolvable_fx=dict(unresolvable_fx or {})
    unsupported={t.root_offset:b for t,b in zip(techniques,bindings) if not b.can_bind_native}
    if not unsupported and not unresolvable_fx:
        return FxOmission(report={'policy':policy,'omitted_effects':[],'omitted_assets':[]})
    if policy=='error':
        parts=[]
        if unsupported:
            parts.append('Unsupported PS3 shaders: '+', '.join(sorted({b.candidate_name for b in unsupported.values()})))
        if unresolvable_fx:
            parts.append('Unprovable FX references: '+'; '.join(
                f'0x{root:X}: {reason}' for root,reason in sorted(unresolvable_fx.items())))
        raise ValueError(' | '.join(parts))
    report=index.report
    if 'asset_edges' not in report or 'named_asset_refs' not in report:raise ValueError('FX omission needs a complete typed dependency graph')
    objects={row['root']:row for row in report['objects']}
    for root in unresolvable_fx:
        row=objects.get(root)
        if row is None or row['type']!='FxEffectDef':
            raise ValueError(f'unresolvable FX root 0x{root:X} is not a typed FxEffectDef in the structural graph')
    edges=defaultdict(set)
    for edge in report['asset_edges']:edges[edge['owner']].add(edge['target'])
    fx_by_name={norm(row['name']):root for root,row in objects.items() if row['type']=='FxEffectDef'}
    for edge in report['named_asset_refs']:
        if edge['type']=='AssetFx' and norm(edge['name']) in fx_by_name:
            edges[edge['owner']].add(fx_by_name[norm(edge['name'])])
    bad=set(unsupported)|set(unresolvable_fx)
    while True:
        parents={root for root,children in edges.items() if children & bad and objects[root]['type']!='FxImpactTable'}
        if parents<=bad:break
        bad|=parents
    blocked=[objects[root] for root in bad if objects[root]['type'] not in ('FxEffectDef','Material','MaterialTechniqueSet')]
    if blocked:raise ValueError('Unsupported shader is also used outside effects: '+', '.join(f"{r['type']} {r['name']}" for r in blocked[:8]))
    fx={root for root in bad if objects[root]['type']=='FxEffectDef'}
    def descendants(seeds):
        result=set(seeds);todo=list(seeds)
        while todo:
            owner=todo.pop()
            for child in edges[owner]-result:
                if objects[owner]['type']=='FxImpactTable' and child in fx:continue
                result.add(child);todo.append(child)
        return result
    candidate=descendants(fx|set(unsupported))
    # World/model roots and all other FX are retained. Materials and images are
    # removable only when no retained structural owner needs them.
    keep_seeds={root for root,row in objects.items() if root not in candidate or (row['type']=='FxEffectDef' and root not in fx)}
    keep=descendants(keep_seeds)
    if keep & set(unsupported):raise ValueError('Unsupported shader remains reachable from a retained asset')
    removable_types={'FxEffectDef','Material','MaterialTechniqueSet','GfxImage'}
    removed={root for root in candidate-keep if objects[root]['type'] in removable_types}
    # Supported descendants may contain a shared native FX; retain it unless it
    # itself reaches an unsupported shader. It has independent runtime meaning.
    removed-= {root for root in removed if objects[root]['type']=='FxEffectDef' and root not in fx}
    names={norm(objects[root]['name']) for root in fx}
    images={norm(objects[root]['name']) for root in removed if objects[root]['type']=='GfxImage'}
    images-={norm(row['name']) for root,row in objects.items() if root not in removed and row['type']=='GfxImage'}
    rows=[dict(type=objects[root]['type'],name=objects[root]['name'],root=root) for root in sorted(removed)]
    indices={row['index'] for row in report['assets'] if row['root'] in removed}
    return FxOmission(removed,indices,names,images,{'policy':policy,
        'unsupported_shaders':sorted({b.candidate_name for b in unsupported.values()}),
        'unresolvable_fx':[{'root':root,'name':objects[root]['name'],'reason':reason}
                           for root,reason in sorted(unresolvable_fx.items())],
        'omitted_effects':sorted(names),'omitted_assets':rows,'omitted_source_indices':sorted(indices),
        'omitted_iwd_image_names':sorted(images),'retained_shared_material_roots':sorted(root for root in candidate & keep if objects[root]['type']=='Material'),'script_changes':[]})
