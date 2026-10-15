"""Query behavior with a minimal game API fixture; not a live-engine test."""
import unittest
from pathlib import Path
try:
    from lupa import LuaRuntime
except ImportError:
    LuaRuntime = None


@unittest.skipUnless(LuaRuntime, 'optional Lua test runtime not installed')
class ScanAreaTests(unittest.TestCase):
    def test_recipe_filter_pagination_and_exact_coordinates(self):
        lua = LuaRuntime()
        lua.execute(Path('mod/scripts/queries.lua').read_text())
        lua.execute('''
        game={tick=123}
        AIPerception={describe_entity=function(e) return {name=e.name,unit_number=e.unit_number} end}
        local entities={}
        for i=1,30 do
          entities[#entities+1]={valid=true,type="transport-belt",name="transport-belt",position={x=i,y=0},unit_number=i,direction=4}
        end
        for i=1,3 do
          entities[#entities+1]={valid=true,type="assembling-machine",name="assembling-machine-1",position={x=40.5+i,y=1.5},unit_number=100+i,direction=0,get_recipe=function() return {name="engine-unit"} end}
        end
        local ctx={force={},surface={name="nauvis",find_entities_filtered=function(filter) return entities end}}
        local a=AIQueries.REGISTRY.scan_area(ctx,{x=0,y=0,radius=100,recipe="engine-unit",limit=2})
        assert(a.total==3 and #a.entities==2 and a.next_offset==2)
        assert(a.entities[1].position.x==41.5 and a.tick==123)
        local b=AIQueries.REGISTRY.scan_area(ctx,{x=0,y=0,radius=100,recipe="engine-unit",limit=2,offset=a.next_offset})
        assert(#b.entities==1 and b.entities[1].unit_number==103 and b.next_offset==nil)
        ''')
