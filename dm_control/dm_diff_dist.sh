#!/bin/bash
#SBATCH --cpus-per-task=1  # Cores proportional to GPUs: 6 on Cedar, 16 on Graham.
#SBATCH --mem=1200M       # Memory proportional to GPUs: 32000 Cedar, 64000 Graham.
#SBATCH --time=0-72:00
#SBATCH --output=%N-%j.out
#SBATCH --account=def-ashique

source $HOME/Documents/ENV/bin/activate
module load python/3.10
module load mujoco mpi4py

SECONDS=0
python Reacher_weighted.py --seed 220  --type "catch" --env "ball_in_cup" --log_dir $SCRATCH/avg_discount/dm_diff/ --epochs 500&
python Reacher_weighted.py --seed 180 --type "easy" --env "point_mass" --log_dir $SCRATCH/avg_discount/dm_diff/ --epochs 500&

echo "Baseline job $seed took $SECONDS"
sleep 72h