import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'dashboard'))
from horizons import event_horizons, STEP


class HorizonTests(unittest.TestCase):
    def bars(self):
        return [{'start_ms':i*STEP,'end_ms':(i+1)*STEP-1,'open':100,'high':104,'low':98,'close':102} for i in range(1,97)]
    def scenario(self,side='BUY',event=STEP):
        return {'side':side,'baseline':{'price':100,'event_ms':event,'method':'event_close_v1'}}
    def test_buy_sell_closed_high_low_horizons(self):
        for side in ('BUY','SELL'):
            r=event_horizons(self.scenario(side),self.bars(),97*STEP)
            self.assertEqual([h['observed_bars'] for h in r['horizons']],[4,16,96])
            for h in r['horizons']:
                self.assertEqual(h['status'],'complete')
                self.assertAlmostEqual(h['mfe_pct'],4 if side=='BUY' else 2)
                self.assertAlmostEqual(h['mae_pct'],-2 if side=='BUY' else -4)
                self.assertEqual(h['mfe_price'],104 if side=='BUY' else 98)
    def test_incomplete_future_missing_duplicates(self):
        b=self.bars();s=self.scenario()
        partial=event_horizons(s,b,2*STEP)['horizons'][0]
        self.assertEqual(partial['status'],'incomplete');self.assertEqual(partial['observed_bars'],1)
        self.assertEqual(event_horizons(s,b+b,5*STEP),event_horizons(s,b,5*STEP))
        self.assertEqual(event_horizons(s,b[1:],5*STEP)['horizons'][0]['status'],'data_unavailable')
        bad=b+[{**b[0],'close':103}]
        self.assertEqual(event_horizons(s,bad,5*STEP)['horizons'][0]['status'],'invalid_data')
    def test_midbar_event_flat_neutral_missing_baseline(self):
        r=event_horizons(self.scenario(event=STEP+1),self.bars(),5*STEP+1)['horizons'][0]
        self.assertEqual(r['observed_bars'],3);self.assertEqual(r['window_start_ms'],2*STEP)
        flat=[{**b,'high':100,'low':100,'close':100} for b in self.bars()]
        r=event_horizons(self.scenario(),flat,5*STEP)['horizons'][0]
        self.assertEqual((r['mfe_pct'],r['mae_pct']),(0,0))
        self.assertEqual(event_horizons(self.scenario(None),flat,5*STEP)['status'],'not_directional')
        self.assertEqual(event_horizons({'side':'BUY'},flat,5*STEP)['status'],'baseline_unavailable')


if __name__=='__main__':unittest.main()
