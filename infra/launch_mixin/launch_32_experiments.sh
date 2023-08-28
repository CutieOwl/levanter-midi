#!/bin/bash

config_dir_2="config/mixin"
config_nums_2=(32 64 128 256 512)

for num in "${config_nums_2[@]}"; do
    config="$config_dir_2/${num}/gpt2_small_${num}_16.yaml"
    echo "Running train_lm.py with $config"
    gcloud compute tpus tpu-vm ssh kathli-nope-32 --zone us-east1-d --worker=all --command="WANDB_API_KEY=9dc3048e28645ac47904ed17c0f13bd782bdd3c7 levanter-midi/infra/launch.sh python levanter-midi/src/levanter/main/train_lm.py --config_path levanter-midi/$config"
    sleep 9000 # 2.5 hours = 2.5 * 60 * 60 = 9000 seconds
done

config_dir="config/mixin/32"
config_nums=(0.25 0.5 1 2 4 8)

for num in "${config_nums[@]}"; do
    config="$config_dir/gpt2_small_32_${num}.yaml"
    echo "Running train_lm.py with $config"
    gcloud compute tpus tpu-vm ssh kathli-nope-32 --zone us-east1-d --worker=all --command="WANDB_API_KEY=9dc3048e28645ac47904ed17c0f13bd782bdd3c7 levanter-midi/infra/launch.sh python levanter-midi/src/levanter/main/train_lm.py --config_path levanter-midi/$config"
    sleep 9000 # 2.5 hours = 2.5 * 60 * 60 = 9000 seconds
done
