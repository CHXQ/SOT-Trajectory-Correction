"""
main.py
Created by zenn at 2021/7/18 15:08
"""
import pytorch_lightning as pl
import argparse

import pytorch_lightning.utilities.distributed
from torch import nn
import yaml
from easydict import EasyDict
import os

from pytorch_lightning.callbacks import ModelCheckpoint
from torch.utils.data import DataLoader
from torch.nn import init

from nusenes_trace_dataset1 import NuscenceTraceDataset, TestTraceDataset
from models.my_model import MyModel
from ipdb import set_trace

# os.environ["NCCL_DEBUG"] = "INFO"
# os.environ['CUDA_VISIBLE_DEVICES'] = "0"
def load_yaml(file_name):
    with open(file_name, 'r') as f:
        try:
            config = yaml.load(f, Loader=yaml.FullLoader)
        except:
            config = yaml.load(f)
    return config


def parse_config():
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch_size', type=int, default=8, help='input batch size')
    parser.add_argument('--epoch', type=int, default=60, help='number of epochs')
    parser.add_argument('--save_top_k', type=int, default=-1, help='save top k checkpoints')
    parser.add_argument('--check_val_every_n_epoch', type=int, default=1, help='check_val_every_n_epoch')
    parser.add_argument('--workers', type=int, default=64, help='number of data loading workers')
    parser.add_argument('--cfg', type=str, default='cfgs/my_model_nuscene.yaml', help='the config_file')
    parser.add_argument('--checkpoint', type=str, default=None, help='checkpoint location')
    parser.add_argument('--log_dir', type=str, default="lightning_logs/point", help='log location')
    parser.add_argument('--test', action='store_true', default=False, help='test mode')
    parser.add_argument('--preloading', action='store_true', default=False, help='preload dataset into memory')
    parser.add_argument('--save_path', type=str, default='outputs')
    args = parser.parse_args()
    config = load_yaml(args.cfg)
    config.update(vars(args))  # override the configuration using the value in args

    return EasyDict(config)


cfg = parse_config()
env_cp = os.environ.copy()
try:
    node_rank, local_rank, world_size = env_cp['NODE_RANK'], env_cp['LOCAL_RANK'], env_cp['WORLD_SIZE']

    is_in_ddp_subprocess = env_cp['PL_IN_DDP_SUBPROCESS']
    pl_trainer_gpus = env_cp['PL_TRAINER_GPUS']
    print(node_rank, local_rank, world_size, is_in_ddp_subprocess, pl_trainer_gpus)

    if int(local_rank) == int(world_size) - 1:
        print(cfg)
except KeyError:
    pass

# init model
 
def initNetParams(net):
    '''Init net parameters.'''
    for m in net.modules():
        if isinstance(m, nn.Conv2d):
            init.xavier_uniform_(m.weight)
            if m.bias:
                init.constant_(m.bias, 0)
        elif isinstance(m, nn.BatchNorm2d):
            init.constant_(m.weight, 1)
            init.constant_(m.bias, 0)
        elif isinstance(m, nn.Conv1d):
            init.xavier_uniform_(m.weight)
            init.constant_(m.bias, 0)
        elif isinstance(m, nn.BatchNorm1d):
            init.constant_(m.weight, 1)
            init.constant_(m.bias, 0)
        elif isinstance(m, nn.Linear):
            init.normal_(m.weight, mean=0, std=1e-3)
            init.constant_(m.bias, 0)

if cfg.checkpoint is None:
    net = MyModel(cfg)
    # initNetParams(net)
else:
    net = MyModel.load_from_checkpoint(cfg.checkpoint, config=cfg)

if not cfg.test:
    # dataset and dataloader
    data_root = '/home/zhangxq/datasets/nuscenes'
    train_ann_file = os.path.join(data_root, 'nuscenes_track_car_train_extract.pkl')
    train_data = NuscenceTraceDataset(data_root, train_ann_file)
    val_ann_file = os.path.join(data_root, 'nuscenes_track_car_val_extract.pkl')
    val_data = TestTraceDataset(data_root, val_ann_file)
    train_loader = DataLoader(train_data, batch_size=cfg.batch_size, num_workers=cfg.workers, shuffle=True,drop_last=True,
                              pin_memory=True)
    val_loader = DataLoader(val_data, batch_size=1, num_workers=cfg.workers, collate_fn=lambda x: x, pin_memory=True)
    checkpoint_callback = ModelCheckpoint(monitor='precision/test', mode='max', save_last=True,
                                          save_top_k=cfg.save_top_k)

    # init trainer
    trainer = pl.Trainer(accelerator='ddp', gpus=-1, max_epochs=cfg.epoch, resume_from_checkpoint=cfg.checkpoint,
                         callbacks=[checkpoint_callback], default_root_dir=cfg.log_dir,
                         check_val_every_n_epoch=cfg.check_val_every_n_epoch, num_sanity_val_steps=2,
                         gradient_clip_val=cfg.gradient_clip_val)
    trainer.fit(net, train_loader, val_loader)
else:
    data_root = '/home/zhangxq/datasets/nuscenes'
    val_ann_file = os.path.join(data_root, 'nuscenes_track_car_val_extract.pkl')
    test_data = TestTraceDataset(data_root, val_ann_file)
    test_loader = DataLoader(test_data, batch_size=1, num_workers=cfg.workers, collate_fn=lambda x: x, pin_memory=True)

    trainer = pl.Trainer(gpus=-1, accelerator='ddp', default_root_dir=cfg.log_dir,
                         resume_from_checkpoint=cfg.checkpoint)
    trainer.test(net, test_loader)
