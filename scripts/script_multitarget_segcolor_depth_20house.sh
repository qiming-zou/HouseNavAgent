#!/bin/bash

CUDA_VISIBLE_DEVICES=5 python3 train.py --seed 0 --algo ddpg_joint --update-freq 10 --max-episode-len 50 \
    --house -20 --reward-type linear --success-measure see --multi-target --use-target-gating \
    --lrate 0.0001 --gamma 0.95 \
    --save-dir ./_model_/multi_target/linear_reward/segcolor_depth_20house_medium/ddpg_joint_gate_100_exp_high_hist_3 \
    --log-dir ./log/multi_target/linear_reward/segcolor_depth_20house_medium/ddpg_joint_gate_100_exp_high_hist_3 \
    --batch-size 256 --hardness 0.6 --replay-buffer-size 1000000 \
    --weight-decay 0.00001 --critic-penalty 0.0001 --entropy-penalty 0.001 \
    --batch-norm --no-debug \
    --noise-scheduler high --q-loss-coef 100 --use-action-gating \
    --segmentation-input color --depth-input --resolution normal --history-frame-len 3
