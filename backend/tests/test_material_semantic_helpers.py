import json
from types import SimpleNamespace

from cod4porter.porter import (
    _output_material_semantics,
    _source_material_semantics,
    _synthetic_material_helper_names,
)


def material(name):
    return SimpleNamespace(name=name)


def assembly(source_names, output_names, *, fx_runtime_visuals=0, xmodel_fallbacks=0):
    # The diagnostics are intentionally varied: helper classification must be semantic and
    # independent of which runtime consumer caused `,default` to be added.
    return SimpleNamespace(
        source=SimpleNamespace(
            materials=SimpleNamespace(
                all_unique=tuple(material(name) for name in source_names),
                xmodel_material_resolution={'runtime_fallbacks':xmodel_fallbacks},
            )
        ),
        material_graph=SimpleNamespace(
            material_symbol_by_name={name: f'material:{name}' for name in output_names}
        ),
        diagnostics={'fx_runtime_material_visuals':fx_runtime_visuals},
    )


def main():
    fx_only=assembly(['brick','metal'],['brick','metal','default'],fx_runtime_visuals=8,xmodel_fallbacks=0)
    assert _source_material_semantics(fx_only)=={'brick','metal'}
    assert _output_material_semantics(fx_only)=={'brick','metal','default'}
    assert _synthetic_material_helper_names(fx_only)=={'default'}

    xmodel_only=assembly(['brick'],['brick','default'],fx_runtime_visuals=0,xmodel_fallbacks=5)
    assert _synthetic_material_helper_names(xmodel_only)=={'default'}

    source_default=assembly(['default','brick'],['default','brick'],fx_runtime_visuals=3,xmodel_fallbacks=2)
    assert _synthetic_material_helper_names(source_default)==set()

    unrelated_extra=assembly(['brick'],['brick','unexpected'],fx_runtime_visuals=0,xmodel_fallbacks=0)
    assert _synthetic_material_helper_names(unrelated_extra)==set()
    assert _output_material_semantics(unrelated_extra)-_source_material_semantics(unrelated_extra)=={'unexpected'}

    print(json.dumps({'passed':True,'cases':4,'fx_only_helper':'default'}))


if __name__=='__main__':
    main()
