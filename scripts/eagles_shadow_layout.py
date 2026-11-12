"""Provisional 2.61 tree slots, used only while the official Wiki tree lacks an ID.

This does not infer research prerequisites. The latest validated Wiki HTML layout
remains authoritative when a new vehicle finally appears in it.
"""
from __future__ import annotations
import json
from pathlib import Path

# Branch = zero-based column in the already established Wiki-style tree.
# Side: left = researchable; right = pack/premium/event/special.
# These branch slots are a temporary positioning layer, not confirmed unlock paths.
BRANCH = {
 'uk_olifant_optimering': ('right', 0),
 'fulmar_mk2': ('left', 2),
 'spad_13': ('right', 0),
 'uk_cruiser_danae_class': ('left', 2),
 'uk_battleship_duke_of_york': ('right', 0),
 'cn_zbd_04': ('left', 3),
 'q_5d': ('right', 0),
 'fr_prtl': ('left', 3),
 'fr_vc1': ('left', 0),
 'fr_l990_monitor': ('right', 0),
 'fr_destroyer_le_hardi_class_le_hardi': ('left', 0),
 'germ_leopard_2a8': ('left', 2),
 'fokker_d7': ('right', 0),
 'germ_battleship_pr_h39': ('left', 0),
 'f_15ia': ('left', 1),
 'it_lancia_flak38': ('left', 2),
 'it_v_4_straussler': ('left', 0),
 'aw_249_pre_serie': ('left', 0),
 'it_destroyer_audace': ('left', 0),
 'jp_bmp_3f': ('left', 0),
 'firefly_mk1_thailand': ('left', 3),
 'b_33_indonesia': ('left', 3),
 'jp_cruiser_takao': ('left', 0),
 'sw_leopard_2a8no': ('left', 1),
 'sw_pv_m39': ('right', 0),
 'saab_jas39_pre_serie': ('right', 0),
 'us_stingray_2': ('right', 0),
 'f_15ex': ('left', 1),
 'fa_18d_late': ('right', 0),
 'us_erie_class': ('left', 0),
 'ussr_btr_90_2004': ('left', 2),
 'su_15tm': ('left', 1),
 'ussr_battleship_imperator_nikolai_pervyi': ('left', 1),
 'ussr_destroyer_vifor_class_lyogkiy': ('right', 0),
}

def _nodes(section):
    for col in section.get('leftCols', []):
        yield from col
    yield from section.get('right', [])

def _node_has_id(node, vehicle_id):
    return node.get('id') == vehicle_id or vehicle_id in (node.get('ids') or [])

def _shift_slot(nodes, pos):
    for node in nodes:
        if int(node.get('row',0)) >= pos:
            node['row'] = int(node.get('row',0)) + 1

def _fix_germany_ps_before_embt(layout):
    rank = ((layout.get('items') or {}).get('Germany|ground') or {}).get('ranks', {}).get('VIII')
    if not rank: return
    cols = rank.get('leftCols', [])
    if len(cols) < 3: return
    pso_id='germ_leopard_2a5_pso'
    embt_id='germ_embt'
    pso = next((n for col in cols for n in col if n.get('id')==pso_id), None)
    embt= next((n for col in cols for n in col if n.get('id')==embt_id), None)
    if pso is None or embt is None: return
    # Official 11.11.2026 progression note: Leopard 2 PSO moved before EMBT.
    # Put both in the same branch (the previous build changed row labels only).
    for col in cols:
        col[:]=[n for n in col if n.get('id') not in (pso_id,embt_id)]
    target=cols[1]
    _shift_slot(target, 0)
    pso['row']=0;pso['col']=1;pso['req']='germ_leopard_2k'
    embt['row']=1;embt['col']=1;embt['req']=pso_id
    pso['source']=embt['source']='official-2.61-PSO-before-EMBT'
    target[:0]=[pso,embt]
    for node in cols[2]:
        if node.get('id')=='germ_leopard_2a7v' and node.get('req')==pso_id:
            node['req']='germ_leopard_2_group'
    # Keep contiguous row values for the remaining Leopard 2A7V / 2A8 chain.
    for col in cols:
        col.sort(key=lambda n:(int(n.get('row',0)),str(n.get('id',''))))
    return True

def apply_layout(data_dir):
    data_dir=Path(data_dir)
    path=data_dir/'tech_tree_layout.json'
    source=data_dir/'eagles_shadow_curated.json'
    if not path.is_file() or not source.is_file(): return (0,0)
    layout=json.loads(path.read_text(encoding='utf-8'))
    curated=json.loads(source.read_text(encoding='utf-8'))['items']
    assert len(curated)==len(BRANCH)==34
    fixed=bool(_fix_germany_ps_before_embt(layout))
    added=0
    for vehicle in curated:
        vid=vehicle['id'];key=vehicle['nation']+'|'+vehicle['class']
        tree=(layout.get('items') or {}).get(key)
        if not tree or tree.get('safe_dom_geometry') is False:continue
        rank=tree.setdefault('ranks',{}).setdefault(vehicle['rank'],{'leftCols':[[],[],[],[],[]],'right':[]})
        # A refreshed official Wiki layout overrides our provisional insertion.
        if any(_node_has_id(n,vid) for n in _nodes(rank)):
            continue
        side,col=BRANCH[vid]
        slot_file = data_dir.parent / 'assets' / 'vehicles' / 'slots' / f'{vid}.png'
        icon = (f'assets/vehicles/slots/{vid}.png' if slot_file.is_file() and slot_file.stat().st_size > 500
                else f'https://static.encyclopedia.warthunder.com/slots/{vid}.png')
        node={'type':'unit','id':vid,'req':'',
              'icon':icon,
              'label':vehicle['nameRu'],
              'source':'eagles-shadow-provisional-branch-until-wiki-sync'}
        if side=='left':
            left=rank.setdefault('leftCols',[[],[],[],[],[]])
            while len(left)<=col:left.append([])
            target=left[col];node['col']=col
            node['row']=max((int(n.get('row',0)) for n in target),default=-1)+1
            # The Leopard 2A8 follows the Leopard 2A7V branch.  Other missing
            # items intentionally do not assert a research prerequisite.
        else:
            target=rank.setdefault('right',[])
            node['col']=col; node['row']=max((int(n.get('row',-1)) for n in target if int(n.get('col',0))==col),default=-1)+1
        target.append(node);added+=1
    path.write_text(json.dumps(layout,ensure_ascii=False,indent=2),encoding='utf-8')
    order_path=data_dir/'tech_tree_order.json'
    if order_path.is_file():
        order=json.loads(order_path.read_text(encoding='utf-8'))
        for vehicle in curated:
            key=vehicle['nation']+'|'+vehicle['class']; arr=order.setdefault('order',{}).setdefault(key,[])
            if vehicle['id'] not in arr:arr.append(vehicle['id'])
        order_path.write_text(json.dumps(order,ensure_ascii=False,indent=2),encoding='utf-8')
    return (added,int(fixed))

if __name__=='__main__':
    print(apply_layout(Path(__file__).resolve().parent.parent/'data'))
