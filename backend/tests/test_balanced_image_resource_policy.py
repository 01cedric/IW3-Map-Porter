from cod4porter.assets.image import ImageResourcePolicy
from cod4porter.material_graph import balanced_resource_policies


def test_balanced_split_is_complete_deterministic_and_indivisible() -> None:
    sizes={'large':90,'medium':60,'small':30,'tiny':10}
    first,bins=balanced_resource_policies(sizes)
    second,bins2=balanced_resource_policies(dict(reversed(tuple(sizes.items()))))
    assert first==second and bins==bins2
    assert set(first)==set(sizes)
    assert set(first.values())=={ImageResourcePolicy.FASTFILE_DELAYED,ImageResourcePolicy.SELF_CONTAINED}
    assert sum(bins)==sum(sizes.values())
    assert abs(bins[0]-bins[1])<=max(sizes.values())


def test_balanced_split_accounts_for_existing_block_load() -> None:
    sizes={'a':90,'b':60,'c':30,'d':10}
    policies,bins=balanced_resource_policies(sizes,base_bytes=(100,10))
    assert policies['a'] is ImageResourcePolicy.SELF_CONTAINED
    assert sum(bins)==100+10+sum(sizes.values())
    assert abs(bins[0]-bins[1])<=max(sizes.values())


if __name__=='__main__':
    test_balanced_split_is_complete_deterministic_and_indivisible()
    print('test_balanced_image_resource_policy PASS')
