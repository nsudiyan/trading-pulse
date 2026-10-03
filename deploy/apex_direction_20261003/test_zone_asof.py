import ast
from math import isfinite
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT/'bot'))
from scenario_contract import zone_observation


def functions(path, names, env):
    nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)


class ZoneAsOf(unittest.TestCase):
    def setUp(self):
        self.env = {'isfinite': isfinite, 'ORDER': ('W','D','240','60','15'),
                    'TF_MS': {'W':604800000,'D':86400000,'240':14400000,'60':3600000,'15':900000}}
        self.env['timeframe_metrics'] = lambda bars, tf: {'trend':'рост' if bars else None,
            'swing_low':90 if bars else None,'swing_high':110 if bars else None,'swing_mid':100 if bars else None}
        functions(ROOT/'bot/setup_layers.py', {'asof_series','zone_snapshot','review_gate'}, self.env)
        self.env.update(equal_level_sweep=lambda bars: {'direction':'BUY','end_ms':bars[-1]['end_ms']},
            asia_range_sweep=lambda *a:None, prior_volume_ratio=lambda bars:3,
            quote_volume_24h=lambda *a,**k:100000000, finding_family=lambda *a:'fixture')
        functions(ROOT/'bot/strong_sweep.py', {'strong_sweep_review'}, self.env)
        self.bar={'start_ms':0,'end_ms':899999,'open':95,'high':95,'low':95,'close':95,'volume':1,'turnover':95}
        self.view=SimpleNamespace(bars={('TEST',tf):[dict(self.bar)] for tf in self.env['ORDER']})

    def call(self, route):
        if route=='review_gate':
            return self.env[route](self.view,'TEST',899999)
        return self.env[route](self.view,'TEST',899999,[{'code':'fixture'}],category='linear',config={'min_independent_families':1})

    def test_both_routes_future_exclusion_replay_idempotence(self):
        for route in ('review_gate','strong_sweep_review'):
            baseline=self.call(route)
            self.assertTrue(baseline['send'])
            for tf in self.env['ORDER']:
                self.view.bars[('TEST',tf)] += [dict(self.bar),{**self.bar,'start_ms':900000,'end_ms':1799999,'close':105}]
            replay=self.call(route)
            self.assertEqual(baseline,replay)
            self.assertEqual(replay['zone'],'discount')
            obs=zone_observation(replay,899999,9999)
            self.assertTrue(obs['event_time_verified'])
            self.assertEqual(obs['observation_price'],95)
            self.assertEqual(obs['asof_end_ms'],899999)
            self.assertEqual(obs['geometry_end_ms'],899999)
            self.assertEqual(obs['source_route'],route)

    def test_missing_and_conflicting_duplicates_fail_closed(self):
        for route in ('review_gate','strong_sweep_review'):
            self.view.bars[('TEST','15')]=[]
            self.assertFalse(self.call(route)['send'])
            self.view.bars[('TEST','15')]=[self.bar,{**self.bar,'close':105}]
            self.assertFalse(self.call(route)['send'])

    def test_flat_equilibrium_is_not_discount(self):
        self.view.bars[('TEST','15')]=[{**self.bar,'close':100}]
        for route in ('review_gate','strong_sweep_review'):
            self.assertFalse(self.call(route)['send'])
        snap=self.env['zone_snapshot']('review_gate',self.view.bars[('TEST','15')][-1],
             {'swing_low':90,'swing_high':110,'swing_mid':100},899999,899999)
        self.assertEqual(snap['zone'],'equilibrium')

    def test_unavailable_geometry_and_future_metadata(self):
        snap=self.env['zone_snapshot']('review_gate',self.bar,{},899999,899999)
        self.assertIsNone(snap['zone'])
        snap=self.call('review_gate')
        snap['zone_provenance']['price_end_ms']=900000
        self.assertFalse(zone_observation(snap,899999,95)['event_time_verified'])


if __name__=='__main__': unittest.main()
