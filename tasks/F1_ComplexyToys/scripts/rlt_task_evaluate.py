#!/usr/bin/env python3
"""Pre-register fixed local trials or summarize reviewed task outcomes.

No robot action, pointer selection or training admission is performed.
"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from f1_toys.rlt_task_evaluation import preregister, preregister_paired, summarize


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    new=sub.add_parser('register');new.add_argument('--identities',type=Path,required=True)
    new.add_argument('--batch-id',required=True);new.add_argument('--output',type=Path,required=True)
    new.add_argument('--reference',choices=['b1250','bc400'],help='Omit for 12-attempt pilot; set for 20-pair comparison')
    report=sub.add_parser('report');report.add_argument('--protocol',type=Path,required=True)
    report.add_argument('--results',type=Path,required=True);report.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.command=='register':
        identities=json.loads(args.identities.read_text())
        value=(preregister_paired(args.batch_id,identities,args.reference) if args.reference else
               preregister(args.batch_id,identities))
    else:value=summarize(json.loads(args.protocol.read_text()),json.loads(args.results.read_text()))
    with args.output.open('x') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n')
    print(args.output)


if __name__=='__main__':main()
