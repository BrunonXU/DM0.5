#!/usr/bin/env python3
"""Plot observed source-vs-IK elbow branches from the coverage diagnosis."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    args=parser.parse_args()
    report=json.loads((args.run/'diagnosis.json').read_text())
    fig,axes=plt.subplots(3,1,figsize=(10,8),sharex=True,layout='constrained')
    for ax,block in zip(axes,report['failed_blocks']):
        x=np.arange(50)/30
        ax.axvspan(0,20/30,alpha=.08,color='green',label='Planned 20-step prefix')
        ax.axhline(60,ls=':',color='gray',label='J4 upper bound')
        ax.plot(x,block['source_j4_deg'],label='Source command J4',color='tab:blue')
        values=np.array(block['returned_j4_deg'])
        valid=np.ones(50,bool);valid[block['failed_steps']]=False
        ax.plot(x,np.where(valid,values,np.nan),label='Accepted IK J4',color='tab:orange')
        ax.scatter(x[~valid],values[~valid],label='Rejected candidate J4',color='tab:red',marker='x',s=18)
        ax.set_title(block['episode']+' / left arm / start=101',fontsize=10)
        ax.set_ylabel('J4 (degrees)');ax.grid(alpha=.2)
    axes[0].legend(ncol=2,fontsize=8,loc='lower left')
    axes[-1].set_xlabel('Time from chunk start (seconds)')
    fig.suptitle('Offline numerical trajectories; no robot motion',fontsize=12)
    target=args.run/'elbow_branch_diagnosis.png'
    if target.exists():raise FileExistsError(target)
    fig.savefig(target,dpi=160)


if __name__=='__main__':main()
