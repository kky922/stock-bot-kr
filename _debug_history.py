#!/usr/bin/env python3
import json, sys

with open("data/agents/autopilot_history.jsonl") as f:
    lines = f.readlines()

for line in lines[-30:]:
    d = json.loads(line.strip())
    ts = d.get('generated_at','')[:16]
    h = d.get('health','')
    m = d.get('metrics',{})
    rp = m.get('realized_pnl',0)
    wr = m.get('win_rate_pct',0)
    op = m.get('open_positions',0)
    cc = m.get('pipeline_candidate_count',0)
    us_pnl = m.get('unrealized_pnl_est',0)
    ins = m.get('issues',[])
    print(f'{ts} | {h:4s} | R+{rp:>7,.0f} | WR{wr:>5.1f} | P:{op} | C:{cc} | U+{us_pnl:>6,.0f} | {ins}')
