# VampNet

This repository contains recipes for training generative music models on top of the Lyrebird Audio Codec.

# Setting up

install 

```bash
git clone https://github.com/hugofloresgarcia/vampnet2.git
pip install -e ./vampnet2
```

## A note on argbind
This repository relies on [argbind](https://github.com/pseeth/argbind) to manage CLIs and config files.
Config files are stored in the `conf/` folder.

## Getting the Pretrained Models

Download the pretrained models from [this link](https://drive.google.com/file/d/1ZIBMJMt8QRE8MYYGjg4lH7v7BLbZneq2/view?usp=sharing). Then, extract the models to the `models/` folder.

# Usage

First, you'll want to set up your environment
```bash
source ./env/env.sh
```

## Launching the Gradio Interface
TODO: add instructions for the new model when we have it? 
```bash
python demo.py --args.load conf/interface/spotdl.yml --Interface.device cuda
```

## Training a model

### Staging a Run

Staging a run makes a copy of all the git-tracked files in the codebase and saves them to a folder for reproducibility. You can then run the training script from the staged folder.

```
stage --name my_run --run_dir /path/to/staging/folder
```

now, run the training script. 

```bash
python scripts/exp/train.py --args.load conf/vampnet.yml --save_path /path/to/checkpoint
```

see `python scripts/exp/train.py -h` for more options.

## Evaluating a model

To evaluate a model, you'll need to generate samples from the test set, then compute metrics on those samples.

### Generating Samples
use the `scripts/utils/vamp_folder.py` script to generate samples from a folder of audio files.

TODO: update interface ckpt
```bash
python scripts/utils/vamp_folder.py  --args.load conf/interface/spotdl.yml --Interface.device cuda --exp_type sampling-steps
```

see `python scripts/utils/vamp_folder.py -h` for more options.

## Fine-tuning
TODO: will the new model need coarse and c2f? should update this script
To fine-tune a model, use the script in `scripts/exp/fine_tune.py` to generate 3 configuration files: `c2f.yml`, `coarse.yml`, and `interface.yml`.
The first two are used to fine-tune the coarse and fine models, respectively. The last one is used to fine-tune the interface.

```bash
python scripts/exp/fine_tune.py "/path/to/audio1.mp3 /path/to/audio2/ /path/to/audio3.wav" <fine_tune_name>
```

This will create a folder under `conf/<fine_tune_name>/` with the 3 configuration files.

The save_paths will be set to `runs/<fine_tune_name>/coarse` and `runs/<fine_tune_name>/c2f`.

launch the coarse job:
```bash
python scripts/exp/train.py --args.load conf/<fine_tune_name>/coarse.yml
```

this will save the coarse model to `runs/<fine_tune_name>/coarse/ckpt/best/`.

launch the c2f job:
```bash
python  scripts/exp/train.py --args.load conf/<fine_tune_name>/c2f.yml
```

launch the interface:
```bash
python  demo.py --args.load conf/generated/<fine_tune_name>/interface.yml
```


