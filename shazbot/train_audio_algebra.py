# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/03_train_audio_algebra.ipynb (unless otherwise specified).

__all__ = ['AudioAlgebra', 'demo', 'get_stems_faders', 'save', 'main']

# Cell
from prefigure.prefigure import get_all_args, push_wandb_config
from copy import deepcopy
import math
import json

import accelerate
import sys
import torch
import torchaudio
from torch import optim, nn
from torch import multiprocessing as mp
from torch.nn import functional as F
from torch.utils import data as torchdata
#from torch.utils import data
from tqdm import tqdm, trange
from einops import rearrange, repeat

import wandb
from .viz import embeddings_table, pca_point_cloud, audio_spectrogram_image, tokens_spectrogram_image
import shazbot.blocks_utils as blocks_utils
from .icebox import load_audio_for_jbx, IceBoxEncoder
from .data import MultiStemDataset

# Cell
class AudioAlgebra(nn.Module):
    def __init__(self, global_args, device):
        super().__init__()
        self.device = device
        self.encoder = IceBoxEncoder(global_args, device)
        self.dims = global_args.latent_dim

        embed_block = nn.Sequential([
            nn.Linear(self.dims,self.dims),
            nn.LeakyReLU(),
            nn.BatchNorm()
            ])

        self.reembedding = nn.Sequential([  # something simple at first
            embed_block,
            embed_block,
            embed_block,
            embed_block,
            embed_block,
            nn.Linear(self.dims,self.dims)
            ])

    def forward(self,
        stems:list,   # list of torch tensors denoting solo audio parts to be mixed together
        faders:list   # list of gain values to be applied to each stem
        ):
        with torch.cuda.amp.autocast():
            zs, zsum = [], torch.zeros((self.dims)).float()
            mix = torch.zeros_like(stems[0]).float()
            for s, f in zip(stems, faders):
                mix_s = s * f             # audio stem adjusted by gain fader f
                with torch.no_grad():
                    z = self.encoder.encode(mix_s).float()  # initial/frozen embedding/latent for that input
                z = self.reembedding(z).float()   # <-- this is the main work of the model
                zsum += z                 # compute the sum of all the z's
                mix += mix_s              # save a record of full audio mix
                zs.append(z)              # save a list of individual z's

            with torch.no_grad():
                zmix = self.encoder.encode(mix).float()  # compute embedding / latent for the full mix

        return zsum, zmix, zs, mix    # zsum = pred, zmix = target,  zs & zmix are just for extra info


    def distance(self, pred, targ):
            return torch.norm( pred - targ ) # L2 / Frobenius / Euclidean

    def loss(self, zsum, zmix):
        with torch.cuda.amp.autocast():
            loss = distance(zsum, zmix)
        log_dict = {'loss': loss.detach()}
        return loss, log_dict

# Cell
def demo():
    print("In demo placeholder")

# Cell
def get_stems_faders(batch, dl):
    "grab some more audio stems and set faders"
    nstems = 1 + int(torch.randint(5,(1,1))[0][0].numpy())
    faders = 2*torch.rand(nstems)-1  # fader gains can be from -1 to 1
    stems = [batch]
    dl_iter = iter(dl)
    for i in range(nstems-1):
        stems.append(next(dl_iter))
    return stems, faders

# Cell
def save(args, model, opt, epoch, step):
    "checkpointing"
    accelerator.wait_for_everyone()
    filename = f'{args.name}_{step:08}.pth'
    if accelerator.is_main_process:
        tqdm.write(f'Saving to {filename}...')
    obj = {
        'model': accelerator.unwrap_model(model).state_dict(),
        'opt': opt.state_dict(),
        'epoch': epoch,
        'step': step
    }
    accelerator.save(obj, filename)

# Cell
def main():

    args = get_all_args()
    torch.manual_seed(args.seed)

    try:
        mp.set_start_method(args.start_method)
    except RuntimeError:
        pass

    accelerator = accelerate.Accelerator()
    device = accelerator.device
    print('Using device:', device, flush=True)

    aa_model = AudioAlgebra(args, device)

    accelerator.print('Parameters:', blocks_utils.n_params(aa_model))

    # If logging to wandb, initialize the run
    use_wandb = accelerator.is_main_process and args.name
    if use_wandb:
        import wandb
        config = vars(args)
        config['params'] = utils.n_params(aa_model)
        wandb.init(project=args.name, config=config, save_code=True)

    opt = optim.Adam([*aa_model.reembedding.parameters()], lr=4e-5)

    train_set = MultiStemDataSet([args.training_dir], args)
    train_dl = torchdata.DataLoader(train_set, args.batch_size, shuffle=True,
                               num_workers=args.num_workers, persistent_workers=True, pin_memory=True)

    aa_model, opt, train_dl = accelerator.prepare(aa_model, opt, train_dl)

    if use_wandb:
        wandb.watch(aa_model)

    if args.ckpt_path:
        ckpt = torch.load(args.ckpt_path, map_location='cpu')
        accelerator.unwrap_model(aa_model).load_state_dict(ckpt['model'])
        opt.load_state_dict(ckpt['opt'])
        epoch = ckpt['epoch'] + 1
        step = ckpt['step'] + 1
        del ckpt
    else:
        epoch = 0
        step = 0

    # all set up, let's go
    try:
        while True:  # training loop
            for batch in tqdm(train_dl, disable=not accelerator.is_main_process):
                opt.zero_grad()

                stems, faders = get_stems_faders(batch, train_dl)
                zsum, zmix, zs, mix = accelerator.unwrap_model(aa_model).forward(stems,faders)
                loss, log_dict = accelerator.unwrap_model(aa_model).loss(zsum, zmix)
                accelerator.backward(loss)
                opt.step()

                if accelerator.is_main_process:
                    if step % 25 == 0:
                        tqdm.write(f'Epoch: {epoch}, step: {step}, loss: {loss.item():g}')

                    if use_wandb:
                        log_dict = {
                            **log_dict,
                            'epoch': epoch,
                            'loss': loss.item(),
                            'lr': sched.get_last_lr()[0],
                        }
                        wandb.log(log_dict, step=step)

                    if step % args.demo_every == 0:
                        demo()

                if step > 0 and step % args.checkpoint_every == 0:
                    save(args, aa_model, opt, epoch, step)

                step += 1
            epoch += 1
    except RuntimeError as err:  # ??
        import requests
        import datetime
        ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        resp = requests.get('http://169.254.169.254/latest/meta-data/instance-id')
        print(f'ERROR at {ts} on {resp.text} {device}: {type(err).__name__}: {err}', flush=True)
        raise err
    except KeyboardInterrupt:
        pass

# Cell
# Not needed if listed in console_scripts in settings.ini
if __name__ == '__main__' and "get_ipython" not in dir():  # don't execute in notebook
    main()
