# From https://colab.research.google.com/drive/1LouqFBIC7pnubCOl5fhnFd33-oVJao2J?usp=sharing#scrollTo=yn1KM6WQ_7Em

"""
python train_online_slim_reverse_img_ddp_update.py  --N 16 --gpu 4,5,6,7       --dir ./runs/cifar10-onlineslim-predstep-1-uniform-shakedrop0.75-beta20/ \
    --weight_prior 20 --learning_rate 2e-4 --dataset cifar10 --warmup_steps 5000  --optimizer adam --batchsize 128 --iterations 650000  \
    --config_en configs/cifar10_en.json --config_de configs/cifar10_de.json --loss_type mse --pred_step 1 --adapt_cu uniform --shakedrop --resume ./runs/cifar10-onlineslim-predstep-1-uniform-shakedrop0.75-beta20/training_state_latest.pth
"""
import torch
import numpy as np
from flows import ConsistencyFlow,OnlineSlimFlow,RectifiedFlow
import torch.nn as nn
import tensorboardX
import os,copy
from models import UNetEncoder
from guided_diffusion.unet import UNetModel
import torchvision.datasets as dsets
from torchvision import transforms
from torchvision.utils import save_image, make_grid
from utils import straightness, get_kl, convert_ddp_state_dict_to_single,LPIPS
from dataset import DatasetWithTraj
import argparse
from tqdm import tqdm
import json 
from EMA import EMA,EMAMODEL
from network_edm import SongUNet,DWTUNet,MetaGenerator
# DDP
import torch.multiprocessing as mp
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group

torch.manual_seed(0)

def ddp_setup(rank, world_size,arg):
    """
    Args:
        rank: Unique identifier of each process
        world_size: Total number of processes
    """
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = f"{12359+int(arg.gpu[0])}"
    # os.environ['CUDA_LAUNCH_BLOCKING'] = "1"
    # Windows
    # init_process_group(backend="gloo", rank=rank, world_size=world_size)
    # Linux
    init_process_group(backend="nccl", rank=rank, world_size=world_size)

def get_args():
    parser = argparse.ArgumentParser(description='Configs')
    parser.add_argument('--gpu', type=str, help='gpu num')
    parser.add_argument('--dir', type=str, help='Saving directory name')
    parser.add_argument('--traj_dir', type=str, help='Trajectory directory name')
    parser.add_argument('--weight_cur', type=float, default = 0, help='Curvature regularization weight')
    parser.add_argument('--iterations', type=int, default = 100000, help='Number of iterations')
    parser.add_argument('--batchsize', type=int, default = 256, help='Batch size')
    parser.add_argument('--learning_rate', type=float, default = 8e-4, help='Learning rate')
    parser.add_argument('--independent', action = 'store_true',  help='Independent assumption, q(x,z) = p(x)p(z)')
    parser.add_argument('--flow_ckpt', type=str, default = None, help='Training state path')
    parser.add_argument('--forward_ckpt', type=str, default = None, help='Training state path')
    parser.add_argument('--pretrain', type=str, default = None, help='Pretrain model state path')
    parser.add_argument('--preforward', type=str, default = None, help='Pretrain forward state path')
    parser.add_argument('--pred_step', type=int, default = 1, help='Predict step')
    parser.add_argument('--N', type=int, default = 16, help='Number of sampling steps')
    parser.add_argument('--num_samples', type=int, default = 64, help='Number of samples to generate')
    parser.add_argument('--no_ema', action='store_true', help='use EMA or not')
    parser.add_argument('--l_weight',type=list, default=[2.,2.],nargs='+', action='append', help='List of numbers')
    parser.add_argument('--ema_after_steps', type=int, default = 1, help='Apply EMA after steps')
    parser.add_argument('--optimizer', type=str, default = 'adamw', help='adam / adamw')
    parser.add_argument('--config_en', type=str, default = None, help='Encoder config path, must be .json file')
    parser.add_argument('--config_de', type=str, default = None, help='Decoder config path, must be .json file')

    arg = parser.parse_args()

    assert arg.dataset in ['cifar10', 'mnist', 'celebahq']
    arg.use_ema = not arg.no_ema
    return arg


def distill(flow_model, forward_model, train_loader, iterations, optimizer, data_shape,device,ema_flow_model=None):
    z_fixed = torch.randn(data_shape, device=device)
    for _num in range(3):
        train_loader.dataset.set_traj((_num+1)*5)
        for i in tqdm(range(iterations+1)):
            optimizer.zero_grad()
            try:
                x = train_iter.next()
            except:
                train_iter = iter(train_loader)
                x = train_iter.next()
            x = x.to(device)
            z,_,_ = forward_model(x, torch.ones((x.shape[0]), device=device))
            # Learn student model
            pred_v = flow_model(z, torch.ones(z.shape[0], device=device))
            pred = z - pred_v
            loss = torch.mean((pred - x)**2)
            if ema_flow_model is not None:
                ema_pred_v = ema_flow_model(z, torch.ones(z.shape[0], device=device))
                loss+=(0.1*torch.mean((pred_v - ema_pred_v)**2))
            loss.backward()
            optimizer.step()
            if ema_flow_model is not None:
                ema_flow_model.ema_step(decay_rate=0.9999,model=flow_model)
            if i % 100 == 0:
                print(f"Iteration {i}: loss {loss.item()}")
            if i % 10000 == 0:
                flow_model.eval()
                with torch.no_grad():
                    pred_v = flow_model(z_fixed, torch.ones(z.shape[0], device=device))
                    pred = z_fixed - pred_v
                    save_image(pred * 0.5 + 0.5, os.path.join(arg.dir, f"pred_{i}.jpg"))
                flow_model.train()
        torch.save(flow_model.state_dict(), os.path.join(arg.dir, f"flow_model_distilled_{_num}.pth"))


def parse_config(config_path):
    with open(config_path, 'r') as f:
        config = json.load(f)
    return config

def main(rank: int, world_size: int, arg):
    ddp_setup(rank, world_size,arg)
    device = torch.device(f"cuda:{rank}")
    input_nc = arg.input_nc
    res = arg.res
    assert arg.config_de is not None
    if not arg.independent:
        assert arg.config_en is not None
        config_en = parse_config(arg.config_en)
    config_de = parse_config(arg.config_de)
    train_dataset = DatasetWithTraj(arg.im_dir, arg.z_dir, input_nc = input_nc)
    data_loader = torch.utils.data.DataLoader(train_dataset, batch_size=arg.batchsize, num_workers=4,sampler=torch.utils.data.distributed.DistributedSampler(train_dataset, num_replicas=world_size, rank=rank))
    data_shape = (arg.batchsize, input_nc, res, res)
    samples_test =  torch.randn((4, input_nc, res, res), device=device)
    
    if not arg.independent:
        if config_en['unet_type'] == 'adm':
            model_class = UNetModel
        elif config_en['unet_type'] == 'songunet':
            model_class = SongUNet
        elif config_en['unet_type'] == 'dwtunet':
            model_class = DWTUNet

        # Pass the arguments in the config file to the model
        encoder = model_class(**config_en)
        forward_model = UNetEncoder(encoder = encoder, input_nc = input_nc)
    else:
        forward_model = None
    # forward_model = torch.compile(forward_model,backend="inductor")

    if arg.pretrain is not None:
        pretrain_state = torch.load(arg.pretrain, map_location = 'cpu')
    config_de['total_N'] = arg.N
    if config_de['unet_type'] == 'adm':
        model_class = UNetModel
    elif config_de['unet_type'] == 'songunet':
        model_class = SongUNet
    elif config_de['unet_type'] == 'dwtunet':
        model_class = DWTUNet

    assert arg.flow_model is not None
    assert arg.forward_model is not None
    flow_model_ckpt = torch.load(arg.flow_model, map_location = 'cpu')
    forward_model_ckpt = torch.load(arg.forward_model, map_location = 'cpu')
    flow_model = model_class(**config_de)
    flow_model.load_state_dict(convert_ddp_state_dict_to_single(flow_model_ckpt))
    forward_model.load_state_dict(convert_ddp_state_dict_to_single(forward_model_ckpt))
    print("Successfully Load Checkpoint!")

    if rank == 0:
        # Print the number of parameters in the model
        print("Begin consistency model training")
        pytorch_total_params = sum(p.numel() for p in flow_model.parameters())
        # Convert to M
        pytorch_total_params = pytorch_total_params / 1000000
        print(f"Total number of the reverse parameters: {pytorch_total_params}M")
        # Save the configuration of flow_model to a json file
        config_dict = flow_model.config
        config_dict['num_params'] = pytorch_total_params
        with open(os.path.join(arg.dir, 'config_flow_model.json'), 'w') as f:
            json.dump(config_dict, f, indent = 4)
        
        # Forward model parameters
        if not arg.independent:
            pytorch_total_params = sum(p.numel() for p in forward_model.parameters())
            # Convert to M
            pytorch_total_params = pytorch_total_params / 1000000
            print(f"Total number of the forward parameters: {pytorch_total_params}M")
            # Save the configuration of encoder to a json file
            config_dict = forward_model.encoder.config if not isinstance(forward_model, DDP) else forward_model.module.encoder.config
            config_dict['num_params'] = pytorch_total_params
            with open(os.path.join(arg.dir, 'config_encoder.json'), 'w') as f:
                json.dump(config_dict, f, indent = 4)
    ################################## FLOW MODEL AND FORWARD MODEL #########################################
    if forward_model is not None:
        forward_model = forward_model.to(device)
        # forward_model = torch.compile(forward_model)
        forward_model = DDP(forward_model, device_ids=[rank])
    flow_model = flow_model.to(device)
    # flow_model = torch.compile(flow_model)
    flow_model = DDP(flow_model, device_ids=[rank])
    if not arg.no_ema:
        ema_flow_model = EMAMODEL(model=flow_model)
    else:
        ema_flow_model = None

    ################################### Learning Optimizer ###################################################
    learnable_params = []
    learnable_params += list(flow_model.parameters())
    if arg.optimizer == 'adamw':
        optimizer = torch.optim.AdamW(learnable_params, lr=arg.learning_rate, weight_decay=0.1, betas = (0.9, 0.9999))
    elif arg.optimizer == 'adam':
        optimizer = torch.optim.Adam(learnable_params, lr=arg.learning_rate, betas = (0.9, 0.999), eps=1e-8)
    else:
        raise NotImplementedError
    if rank==0:
        print(f"Start training")
        
    distill(flow_model, forward_model, data_loader, arg.iterations, optimizer, data_shape,device,ema_flow_model=ema_flow_model)
    destroy_process_group()

if __name__ == "__main__":
    arg = get_args()
    if not os.path.exists(arg.dir):
        os.makedirs(arg.dir)
    os.environ["CUDA_VISIBLE_DEVICES"] = arg.gpu
    device_ids = arg.gpu.split(',')
    device_ids = [int(i) for i in device_ids]
    world_size = len(device_ids)
    with open(os.path.join(arg.dir, "config.json"), "w") as json_file:
        json.dump(vars(arg), json_file, indent = 4)
    arg.batchsize = arg.batchsize // world_size
    try:
       mp.spawn(main, args=(world_size, arg), nprocs=world_size)
    except KeyboardInterrupt:
        print("KeyboardInterrupt")
        destroy_process_group()
        exit(0)
