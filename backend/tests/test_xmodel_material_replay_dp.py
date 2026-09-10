import itertools,json,random
from cod4porter.pc_xmodel import _best_two_monotonic_assignments,_transition_block4_score


def exhaustive(targets,lists):
    rows=[]
    for combo in itertools.product(*lists):
        cells=[x['cell'] for x in combo]
        if any(cells[i]>=cells[i+1] for i in range(len(cells)-1)):continue
        model_bases={};consistent=True
        for row in combo:
            mi=row['model_index']
            if mi in model_bases and model_bases[mi]!=row['base']:consistent=False;break
            model_bases[mi]=row['base']
        if not consistent:continue
        score=sum(float(x['pair_score']) for x in combo)
        for i in range(len(combo)-1):
            score+=_transition_block4_score(combo[i],combo[i+1],targets[i+1]-targets[i])
        rows.append((score,min(float(x['pair_score']) for x in combo),combo))
    rows.sort(key=lambda row:row[0],reverse=True)
    return rows


def main():
    rng=random.Random(0xC019)
    cases=400
    for case in range(cases):
        count=rng.randint(1,5);target=0x1000;targets=[];lists=[]
        for layer in range(count):
            target+=rng.choice((4,8,12));targets.append(target);candidates=[]
            for ci in range(rng.randint(1,5)):
                model=rng.randint(0,5);surface=rng.randint(0,4)
                candidates.append({
                    'model_index':model,'model':f'm{model}','surface':surface,
                    'name':f'n{layer}_{ci}','cell':(model,surface),
                    'base':target-surface*4,'pair_score':rng.randint(-5,25),
                })
            candidates.sort(key=lambda row:row['cell']);lists.append(candidates)
        expected=exhaustive(targets,lists)
        actual,valid,saturated=_best_two_monotonic_assignments(targets,lists)
        assert not saturated
        assert valid==len(expected)
        assert [row[0] for row in actual]==[row[0] for row in expected[:2]],case
        assert [row[1] for row in actual]==[row[1] for row in expected[:2]],case
    print(json.dumps({'passed':True,'random_cases':cases,'algorithm':'layered-dag-top2','oracle':'exhaustive-cartesian'},indent=2))


if __name__=='__main__':main()
