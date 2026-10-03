import ast, asyncio, copy, json, sqlite3, sys, unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'bot'))
from scenario_contract import classify, ensure_schema, save, zone_observation
from provenance_guard import guarded_quality, verified_zone
import test_zone_asof


class GuardTests(unittest.TestCase):
    def scenario(self):
        fixture=test_zone_asof.ZoneAsOf();fixture.setUp()
        zone=zone_observation(fixture.call('review_gate'),899999,95)
        return classify([],{},899999,zone=zone)

    def test_quality_matrix(self):
        good=self.scenario()
        self.assertTrue(guarded_quality({'send':True},good)['send'])
        for variant in ('false','unavailable','future','missing','geometry'):
            bad=copy.deepcopy(good);z=bad['zone_observation']
            if variant=='false':z['event_time_verified']=False
            if variant=='unavailable':z['status']='unavailable'
            if variant=='future':z['asof_end_ms']=900000
            if variant=='missing':bad.pop('zone_observation')
            if variant=='geometry':z['geometry']['swing_mid']=999
            for side in ('BUY','SELL',None):
                bad['side']=side
                self.assertFalse(guarded_quality({'send':True},bad)['send'])
            rejected={'send':False,'reason':'existing_gate_failure'}
            self.assertEqual(guarded_quality(rejected,bad),rejected)

    def test_actual_sender_fail_closed_and_duplicate(self):
        node=next(n for n in ast.walk(ast.parse((ROOT/'bot/market.py').read_text())) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_flush_locked')
        node.returns=None
        for arg in node.args.args:arg.annotation=None
        sent=[]
        async def fake_send(*args):sent.append(args[-1])
        env={'json':json,'verified_zone':verified_zone,'utc_now':lambda:datetime.now(timezone.utc),
             'review_is_stale_for_delivery':lambda *a:False,'parse_time':datetime.fromisoformat,
             'os':SimpleNamespace(environ={'token':'fixture','chat':'fixture'}),'send_telegram':fake_send}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'actual_market_sender','exec'),env)
        for mode in ('valid','false','unavailable','missing'):
            db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE signal_alerts (id TEXT PRIMARY KEY,status TEXT,text TEXT,created_utc TEXT,sent_utc TEXT,reason TEXT)')
            ensure_schema(db)
            s=self.scenario()
            if mode=='false':s['zone_observation']['event_time_verified']=False
            if mode=='unavailable':s['zone_observation']['status']='unavailable'
            if mode!='missing':save(db,'fixture',s)
            for _ in range(2): db.execute('INSERT OR IGNORE INTO signal_alerts VALUES (?,?,?,?,?,?)',('fixture','pending','test only',datetime.now(timezone.utc).isoformat(),None,'fixture'))
            obj=SimpleNamespace(store=SimpleNamespace(db=db),config={'telegram':{'enabled':True,'token_env':'token','chat_id_env':'chat'}},market={},previewed=set())
            before=len(sent)
            for _ in range(2):asyncio.run(env['_flush_locked'](obj,None,False))
            self.assertEqual(len(sent)-before,1 if mode=='valid' else 0)
            self.assertEqual(db.execute('SELECT status FROM signal_alerts').fetchone()[0],'sent' if mode=='valid' else 'suppressed_provenance')
            self.assertEqual(db.execute('SELECT count(*) FROM signal_alerts').fetchone()[0],1)
            db.close()


if __name__=='__main__':unittest.main()
