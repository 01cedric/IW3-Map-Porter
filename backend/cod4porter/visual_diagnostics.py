"""Map-independent source pixel coverage; no inferred image substitutions."""
from collections import defaultdict
from .material_graph import norm
from .pc_image import parse_image_at


def source_pixel_coverage(source):
    users = defaultdict(set)
    embedded = set()
    diffuse_users = defaultdict(set)
    neutral_bindings = []
    for p in source.materials.planned_owned:
        for texture, name in zip(p.source.textures, p.image_names):
            key = norm(name).casefold()
            users[key].add(norm(p.source.name))
            if texture.name_hash == 0xA0AB1041 or texture.semantic == 2:
                diffuse_users[key].add(norm(p.source.name))
            if key.startswith('__cp11_neutral_semantic_'):
                neutral_bindings.append({'material': norm(p.source.name),
                                         'sampler_hash': f'0x{texture.name_hash:08x}',
                                         'source_pointer': f'0x{texture.resource_pointer:08x}',
                                         'reason': 'Source image identity remains unresolved.'})
            if texture.inline_image_root is not None:
                image = parse_image_at(source.pc_document.zone, texture.inline_image_root)
                if image.owns_resource:
                    embedded.add(key)
    embedded.update(norm(r.name).casefold() for r in source.image_catalog.records_by_asset_index.values() if r.owns_resource)
    iwd = {norm(k).casefold() for k in source.iwd_images}
    synthetic={key for key in users if key.startswith('__cp11_neutral_semantic_')}
    engine={key for key in users if key.startswith('$')}
    missing = sorted(set(users) - iwd - embedded - synthetic - engine)
    return {'map': source.map_name, 'required_image_names': len(users),
            'images_with_source_pixels': len(set(users) & (iwd | embedded)),
            'images_without_source_pixels': len(missing),
            'synthetic_neutral_images':sorted(synthetic),'engine_images':sorted(engine),
            'synthetic_neutral_bindings': neutral_bindings,
            'missing_diffuse_images': [{'image': key, 'materials': sorted(diffuse_users[key])}
                                      for key in missing if key in diffuse_users],
            'missing': [{'image': name, 'materials': sorted(users[name]),
                         'engine_image': name.startswith('$')}
                        for name in missing],
            'note': 'No source pixels means an external PS3 image is required. Target support-zone linking determines whether it exists. Supply the original PC IWDs for unavailable names; placeholders cannot recover texture detail or alpha.',
            'technique_substitutions': [{'material': norm(p.source.name),
                                        'source': p.technique.source_name,
                                        'target': p.technique.candidate_name,
                                        'reason': p.technique.reason}
                                       for p in source.materials.planned_owned if p.technique.substitution]}
