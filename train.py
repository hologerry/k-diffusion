#!/usr/bin/env python3

import argparse
from copy import deepcopy
import math
from pathlib import Path

import accelerate
import torch
from torch import optim
from torch.utils import data
from torchvision import datasets, transforms, utils as tv_utils
from tqdm import trange, tqdm

from k_diffusion import evaluation, gns, layers, models, sampling, utils


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--batch-size', type=int, default=64,
                   help='the batch size')
    p.add_argument('--demo-every', type=int, default=500,
                   help='save a demo grid every this many steps')
    p.add_argument('--evaluate-every', type=int, default=10000,
                   help='save a demo grid every this many steps')
    p.add_argument('--evaluate-n', type=int, default=2000,
                   help='the number of samples to draw to evaluate')
    p.add_argument('--gns', action='store_true',
                   help='measure the gradient noise scale (DDP only)')
    p.add_argument('--lr', type=float, default=3e-4,
                   help='the learning rate')
    p.add_argument('--n-to-sample', type=int, default=64,
                   help='the number of images to sample for demo grids')
    p.add_argument('--name', type=str, default='model',
                   help='the name of the run')
    p.add_argument('--resume', type=str, 
                   help='the checkpoint to resume from')
    p.add_argument('--save-every', type=int, default=10000,
                   help='save every this many steps')
    p.add_argument('--size', type=int, default=32,
                   help='the image size')
    p.add_argument('--train-set', type=str, required=True,
                   help='the training set location')
    p.add_argument('--wandb-entity', type=str,
                   help='the wandb entity name')
    p.add_argument('--wandb-group', type=str,
                   help='the wandb group name')
    p.add_argument('--wandb-project', type=str,
                   help='the wandb project name (specify this to enable wandb)')
    p.add_argument('--wandb-save-model', action='store_true',
                   help='save model to wandb')

    args = p.parse_args()

    accelerator = accelerate.Accelerator()
    device = accelerator.device
    print('Using device:', device, flush=True)

    n_layers = math.ceil(math.log2(args.size)) - 2
    depths = [2] * (n_layers - 2) + [4, 4]
    channels = [128] * (n_layers - 2) + [256, 512]
    self_attn_depths = [False] * (n_layers - 2) + [True, True]
    inner_model = models.ImageDenoiserInnerModel(3, 128, depths, channels, self_attn_depths)
    accelerator.print('Parameters:', utils.n_params(inner_model))

    # If logging to wandb, initialize the run
    use_wandb = accelerator.is_main_process and args.wandb_project
    if use_wandb:
        import wandb
        config = vars(args)
        config['params'] = utils.n_params(inner_model)
        wandb.init(project=args.wandb_project, entity=args.wandb_entity, group=args.wandb_group, config=config, save_code=True)

    opt = optim.Adam(inner_model.parameters(), lr=args.lr, betas=(0.95, 0.999))
    sched = utils.InverseLR(opt, inv_gamma=50000, power=1/2, warmup=0.99)
    ema_sched = utils.EMAWarmup(power=3/4, max_value=0.999)

    tf = transforms.Compose([
        transforms.Resize(args.size, interpolation=transforms.InterpolationMode.LANCZOS),
        transforms.CenterCrop(args.size),
        transforms.ToTensor(),
        transforms.Normalize([0.5], [0.5])
    ])
    train_set = datasets.ImageFolder(args.train_set, transform=tf)
    train_dl = data.DataLoader(train_set, args.batch_size, shuffle=True, drop_last=True,
                               num_workers=16, persistent_workers=True)

    inner_model, opt, train_dl = accelerator.prepare(inner_model, opt, train_dl)
    if use_wandb:
        wandb.watch(inner_model)
    if args.gns:
        gns_stats_hook = gns.DDPGradientStatsHook(inner_model)
        gns_stats = gns.GradientNoiseScale()
    else:
        gns_stats = None
    model = layers.Denoiser(inner_model, sigma_data=0.5)
    model_ema = deepcopy(model)

    if args.resume:
        ckpt = torch.load(args.resume, map_location='cpu')
        accelerator.unwrap_model(model.inner_model).load_state_dict(ckpt['model'])
        accelerator.unwrap_model(model_ema.inner_model).load_state_dict(ckpt['model_ema'])
        opt.load_state_dict(ckpt['opt'])
        sched.load_state_dict(ckpt['sched'])
        ema_sched.load_state_dict(ckpt['ema_sched'])
        epoch = ckpt['epoch'] + 1
        step = ckpt['step'] + 1
        if args.gns and 'gns_stats' in ckpt and ckpt['gns_stats'] is not None:
            gns_stats.load_state_dict(ckpt['gns_stats'])
        del ckpt
    else:
        epoch = 0
        step = 0

    extractor = evaluation.InceptionV3FeatureExtractor(device=device)
    train_iter = iter(train_dl)
    accelerator.print('Computing features for reals...')
    reals_features = evaluation.compute_features(accelerator, lambda x: next(train_iter)[0], extractor, args.evaluate_n, args.batch_size)
    if accelerator.is_main_process:
        metrics_log_filepath = Path(f'{args.name}_metrics.csv')
        if metrics_log_filepath.exists():
            metrics_log_file = open(metrics_log_filepath, 'a')
        else:
            metrics_log_file = open(metrics_log_filepath, 'w')
            print('step', 'fid', 'kid', sep=',', file=metrics_log_file, flush=True)

    sigma_min, sigma_max = 1e-2, 80
    sigma_mean, sigma_std = -1.2, 1.2

    @torch.no_grad()
    @utils.eval_mode(model_ema)
    def demo():
        if accelerator.is_local_main_process:
            tqdm.write('Sampling...')
        filename = f'{args.name}_demo_{step:08}.png'
        n_per_proc = math.ceil(args.n_to_sample / accelerator.num_processes)
        x = torch.randn([n_per_proc, 3, args.size, args.size], device=device) * sigma_max
        sigmas = sampling.get_sigmas_karras(50, sigma_min, sigma_max, rho=7., device=device)
        x_0 = sampling.sample_lms(model_ema, x, sigmas, disable=not accelerator.is_local_main_process)
        x_0 = accelerator.gather(x_0)[:args.n_to_sample]
        if accelerator.is_main_process:
            grid = tv_utils.make_grid(x_0, nrow=math.ceil(args.n_to_sample ** 0.5), padding=0)
            utils.to_pil_image(grid).save(filename)
            if use_wandb:
                wandb.log({'demo_grid': wandb.Image(filename)}, step=step)

    @torch.no_grad()
    @utils.eval_mode(model_ema)
    def evaluate():
        if accelerator.is_local_main_process:
            tqdm.write('Evaluating...')
        sigmas = sampling.get_sigmas_karras(50, sigma_min, sigma_max, rho=7., device=device)
        def sample_fn(n):
            x = torch.randn([n, 3, args.size, args.size], device=device) * sigma_max
            x_0 = sampling.sample_lms(model_ema, x, sigmas, disable=True)
            return x_0
        fakes_features = evaluation.compute_features(accelerator, sample_fn, extractor, args.evaluate_n, args.batch_size)
        fid = evaluation.fid(fakes_features, reals_features)
        kid = evaluation.kid(fakes_features, reals_features)
        accelerator.print(f'FID: {fid.item():g}, KID: {kid.item():g}')
        if accelerator.is_main_process:
            print(step, fid.item(), kid.item(), sep=',', file=metrics_log_file, flush=True)
        if use_wandb:
            wandb.log({'FID': fid.item(), 'KID': kid.item()}, step=step)

    def save():
        accelerator.wait_for_everyone()
        filename = f'{args.name}_{step:08}.pth'
        if accelerator.is_local_main_process:
            tqdm.write(f'Saving to {filename}...')
        obj = {
            'model': accelerator.unwrap_model(model.inner_model).state_dict(),
            'model_ema': accelerator.unwrap_model(model_ema.inner_model).state_dict(),
            'opt': opt.state_dict(),
            'sched': sched.state_dict(),
            'ema_sched': ema_sched.state_dict(),
            'epoch': epoch,
            'step': step,
            'gns_stats': gns_stats.state_dict() if gns_stats is not None else None,
        }
        accelerator.save(obj, filename)
        if args.wandb_save_model and use_wandb:
            wandb.save(filename)

    while True:
        for batch in tqdm(train_dl, disable=not accelerator.is_local_main_process):
            opt.zero_grad()
            reals = batch[0].to(device)
            noise = torch.randn_like(reals)
            sigma = torch.distributions.LogNormal(sigma_mean, sigma_std).sample([reals.shape[0]]).to(device)
            loss = model.loss(reals, noise, sigma).mean()
            accelerator.backward(loss)
            if args.gns:
                sq_norm_small_batch, sq_norm_large_batch = accelerator.gather(gns_stats_hook.get_stats()).mean(0).tolist()
                gns_stats.update(sq_norm_small_batch, sq_norm_large_batch, reals.shape[0], reals.shape[0] * accelerator.num_processes)
            opt.step()
            sched.step()
            ema_decay = ema_sched.get_value()
            utils.ema_update(model, model_ema, ema_decay)
            ema_sched.step()

            if accelerator.is_local_main_process:                    
                if step % 25 == 0:
                    if args.gns:
                        tqdm.write(f'Epoch: {epoch}, step: {step}, loss: {loss.item():g}, gns: {gns_stats.get_gns():g}')
                    else:
                        tqdm.write(f'Epoch: {epoch}, step: {step}, loss: {loss.item():g}')

            if use_wandb:
                log_dict = {
                    'epoch': epoch,
                    'loss': loss.item(),
                    'lr': sched.get_last_lr()[0],
                    'ema_decay': ema_decay,
                }
                if args.gns:
                    log_dict['gradient_noise_scale'] = gns_stats.get_gns()
                wandb.log(log_dict, step=step)

            if step % args.demo_every == 0:
                demo()

            if step > 0 and args.evaluate_every > 0 and step % args.evaluate_every == 0:
                evaluate()

            if step > 0 and step % args.save_every == 0:
                save()

            step += 1
        epoch += 1


if __name__ == '__main__':
    main()
