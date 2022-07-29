# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/train_aa_mixer.ipynb (unless otherwise specified).

__all__ = ['DiffusionDVAE', 'setup_weights', 'ad_encode_it', 'EmbedBlock', 'AudioAlgebra', 'get_alphas_sigmas',
           'get_crash_schedule', 'alpha_sigma_to_t', 'sample', 'make_eps_model_fn', 'make_autocast_model_fn',
           'transfer', 'prk_step', 'plms_step', 'prk_sample', 'plms_sample', 'pie_step', 'plms2_step', 'pie_sample',
           'plms2_sample', 'make_cond_model_fn', 'demo', 'get_stems_faders', 'main']

# Cell
from prefigure.prefigure import get_all_args, push_wandb_config
from copy import deepcopy
import math
import json

import accelerate
import os, sys
import torch
import torchaudio
from torch import optim, nn, Tensor
from torch import multiprocessing as mp
from torch.nn import functional as F
from torch.utils import data as torchdata
#from torch.utils import data
from tqdm import tqdm, trange
from einops import rearrange, repeat

import wandb
import subprocess

from .viz import embeddings_table, pca_point_cloud, audio_spectrogram_image, tokens_spectrogram_image
from .core import n_params, save, freeze, HostPrinter, Mish
#import shazbot.blocks_utils as blocks_utils
from .icebox import load_audio_for_jbx, IceBoxEncoder
from .data import MultiStemDataset


# audio-diffusion imports
from tqdm import trange
import pytorch_lightning as pl
from diffusion.pqmf import CachedPQMF as PQMF
from diffusion.utils import PadCrop, Stereo, NormInputs
from encoders.encoders import RAVEEncoder, ResConvBlock
from nwt_pytorch import Memcodes
from dvae.residual_memcodes import ResidualMemcodes
from decoders.diffusion_decoder import DiffusionDecoder

# Cell
#audio diffusion classes
class DiffusionDVAE(nn.Module):
    def __init__(self, global_args, device):
        super().__init__()
        self.device = device

        self.pqmf_bands = global_args.pqmf_bands

        if self.pqmf_bands > 1:
            self.pqmf = PQMF(2, 70, global_args.pqmf_bands)

        self.encoder = RAVEEncoder(2 * global_args.pqmf_bands, 64, global_args.latent_dim, ratios=[2, 2, 2, 2, 4, 4])
        self.encoder_ema = deepcopy(self.encoder)

        self.diffusion = DiffusionDecoder(global_args.latent_dim, 2)
        self.diffusion_ema = deepcopy(self.diffusion)
        self.rng = torch.quasirandom.SobolEngine(1, scramble=True)
        #self.ema_decay = global_args.ema_decay

        self.num_quantizers = global_args.num_quantizers
        if self.num_quantizers > 0:
            quantizer_class = ResidualMemcodes if global_args.num_quantizers > 1 else Memcodes

            quantizer_kwargs = {}
            if global_args.num_quantizers > 1:
                quantizer_kwargs["num_quantizers"] = global_args.num_quantizers

            self.quantizer = quantizer_class(
                dim=global_args.latent_dim,
                heads=global_args.num_heads,
                num_codes=global_args.codebook_size,
                temperature=1.,
                **quantizer_kwargs
            )

            self.quantizer_ema = deepcopy(self.quantizer)



    def encode(self, *args, **kwargs):
        if self.training:
            return self.encoder(*args, **kwargs)
        return self.encoder_ema(*args, **kwargs)

    def decode(self, *args, **kwargs):
        if self.training:
            return self.diffusion(*args, **kwargs)
        return self.diffusion_ema(*args, **kwargs)

    def configure_optimizers(self):
        return optim.Adam([*self.encoder.parameters(), *self.diffusion.parameters()], lr=2e-5)


    def training_step(self, batch, batch_idx):
        reals = batch[0]

        encoder_input = reals

        if self.pqmf_bands > 1:
            encoder_input = self.pqmf(reals)

        # Draw uniformly distributed continuous timesteps
        t = self.rng.draw(reals.shape[0])[:, 0].to(self.device)

        # Calculate the noise schedule parameters for those timesteps
        alphas, sigmas = get_alphas_sigmas(get_crash_schedule(t))

        # Combine the ground truth images and the noise
        alphas = alphas[:, None, None]
        sigmas = sigmas[:, None, None]
        noise = torch.randn_like(reals)
        noised_reals = reals * alphas + noise * sigmas
        targets = noise * alphas - reals * sigmas

        # Compute the model output and the loss.
        with torch.cuda.amp.autocast():
            tokens = self.encoder(encoder_input).float()

        if self.num_quantizers > 0:
            #Rearrange for Memcodes
            tokens = rearrange(tokens, 'b d n -> b n d')

            #Quantize into memcodes
            tokens, _ = self.quantizer(tokens)

            tokens = rearrange(tokens, 'b n d -> b d n')

        with torch.cuda.amp.autocast():
            v = self.diffusion(noised_reals, t, tokens)
            mse_loss = F.mse_loss(v, targets)
            loss = mse_loss

        log_dict = {
            'train/loss': loss.detach(),
            'train/mse_loss': mse_loss.detach(),
        }

        self.log_dict(log_dict, prog_bar=True, on_step=True)
        return loss

        '''def on_before_zero_grad(self, *args, **kwargs):
        decay = 0.95 if self.current_epoch < 25 else self.ema_decay
        ema_update(self.diffusion, self.diffusion_ema, decay)
        ema_update(self.encoder, self.encoder_ema, decay)

        if self.num_quantizers > 0:
            ema_update(self.quantizer, self.quantizer_ema, decay)'''


def setup_weights(model, accelerator):
    pthfile = 'dvae-checkpoint-june9.pth'
    if not os.path.exists(pthfile):
        cmd = f'curl -C - -LO https://www.dropbox.com/s/8tcirpokhoxfo82/{pthfile}'
        process = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE)
        output, error = process.communicate()
    #self.load_state_dict(torch.load(pthfile))
    accelerator.unwrap_model(model).load_state_dict(torch.load(pthfile))
    model = model.to(accelerator.device)
    return model

def ad_encode_it(reals, device, dvaemodel, sample_size=32768, num_quantizers=8):
    encoder_input = reals.to(device)
    noise = torch.randn([reals.shape[0], 2, sample_size]).to(device)

    tokens = dvaemodel.encoder_ema(encoder_input)
    if num_quantizers > 0:
        #Rearrange for Memcodes
        tokens = rearrange(tokens, 'b d n -> b n d')
        tokens, _= dvaemodel.quantizer_ema(tokens)
        tokens = rearrange(tokens, 'b n d -> b d n')

    return tokens

# Cell
class EmbedBlock(nn.Module):
    def __init__(self, dims:int, **kwargs) -> None:
        super().__init__()
        self.lin = nn.Linear(dims, dims, **kwargs)
        #self.act = nn.LeakyReLU()
        self.act = Mish()
        self.bn = nn.BatchNorm1d(dims)

    def forward(self, x: Tensor) -> Tensor:
        x = self.lin(x)
        x = rearrange(x, 'b d n -> b n d') # gotta rearrange for bn
        x = self.bn(x)
        x = rearrange(x, 'b n d -> b d n') # and undo rearrange for later layers
        return self.act(x)


class AudioAlgebra(nn.Module):
    def __init__(self, global_args, device, enc_model):
        super().__init__()
        self.device = device
        #self.encoder = encoder
        self.enc_model = enc_model
        self.dims = global_args.latent_dim
        self.sample_size = global_args.sample_size
        self.num_quantizers = global_args.num_quantizers

        self.reembedding = nn.Sequential(  # something simple at first
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            EmbedBlock(self.dims),
            nn.Linear(self.dims,self.dims)
            )

    def forward(self,
        stems:list,   # list of torch tensors denoting (chunked) solo audio parts to be mixed together
        faders:list   # list of gain values to be applied to each stem
        ):
        """We're going to 'on the fly' mix the stems according to the fader settings and generate
        frozen-encoder embeddings for each (fader-adjusted) stem and for the total mix.
        "z0" denotes an embedding from the frozen encoder, "z" denotes re-mapped embeddings
        in (hopefully) the learned vector space"""
        with torch.cuda.amp.autocast():
            zs, z0s, zsum = [], [], None
            mix = torch.zeros_like(stems[0]).float()
            #print("mix.shape = ",mix.shape)
            for s, f in zip(stems, faders):
                mix_s = s * f             # audio stem adjusted by gain fader f
                with torch.no_grad():
                    #z0 = self.encoder.encode(mix_s).float()  # initial/frozen embedding/latent for that input
                    z0 = ad_encode_it(mix_s, self.device, self.enc_model, sample_size=self.sample_size, num_quantizers=self.num_quantizers)
                #print("z0.shape = ",z0.shape)  # most likely [8,32,152]
                z0 = rearrange(z0, 'b d n -> b n d')
                z = self.reembedding(z0).float()   # <-- this is the main work of the model
                zsum = z if zsum is None else zsum + z # compute the sum of all the z's. we'll end up using this in our (metric) loss as "pred"
                mix += mix_s              # save a record of full audio mix
                zs.append(z)              # save a list of individual z's
                z0s.append(z0)            # save a list of individual z0's

            with torch.no_grad():
                #zmix0 = self.encoder.encode(mix).float()  # compute frozen embedding / latent for the full mix
                zmix0 = ad_encode_it(mix, self.device, self.enc_model, sample_size=self.sample_size, num_quantizers=self.num_quantizers)
            zmix0 = rearrange(zmix0, 'b d n -> b n d')
            zmix = self.reembedding(zmix0).float()        # map that according to our learned re-embedding. this will be the "target" in the metric loss

            archive = {'zs':zs, 'mix':mix, 'znegsum':None, 'z0s': z0s}

        return zsum, zmix, archive    # zsum = pred, zmix = target, and "archive" of extra stuff zs & zmix are just for extra info

    def mag(self, v):
        return torch.norm( v, dim=(1,2) ) # L2 / Frobenius / Euclidean

    def distance(self, pred, targ):
        return self.mag(pred - targ)


    def loss(self, zsum, zmix, archive, margin=1.0, loss_type='noshrink'):
        with torch.cuda.amp.autocast():
            dist = self.distance(zsum, zmix) # for each member of batch, compute distance
            loss = (dist**2).mean()  # mean across batch; so loss range doesn't change w/ batch_size hyperparam
            #print("dist = ",dist)
            #dist = rearrange(dist, 'b d n -> b (d n)') # flatten non-batch parts
            if ('triplet'==loss_type) and (archive['znegsum'] is not None):
                negdist = self.distance(archive['znegsum'], zmix)
                negdist = negdist * (negdist < margin)   # beyond margin, do nothing
                loss = F.relu( (dist**2).mean() - (negdist**2).mean() ) # relu gets us hinge of L2
            if ('noshrink' == loss_type):     # try to preserve original magnitudes of of vectors
                magdiffs2 = [ ( self.mag(z) - self.mag(z0) )**2 for (z,z0) in zip(archive['zs'], archive['z0s']) ]
                loss += 1/300*(sum(magdiffs2)/len(magdiffs2)).mean() # mean of l2 of diff in vector mag  extra .mean() for good measure
        return loss

# Cell

# Define the noise schedule and sampling loop
def get_alphas_sigmas(t):
    """Returns the scaling factors for the clean image (alpha) and for the
    noise (sigma), given a timestep."""
    return torch.cos(t * math.pi / 2), torch.sin(t * math.pi / 2)


def get_crash_schedule(t):
    sigma = torch.sin(t * math.pi / 2) ** 2
    alpha = (1 - sigma ** 2) ** 0.5
    return alpha_sigma_to_t(alpha, sigma)


def alpha_sigma_to_t(alpha, sigma):
    """Returns a timestep, given the scaling factors for the clean image and for
    the noise."""
    return torch.atan2(sigma, alpha) / math.pi * 2


@torch.no_grad()
def sample(model, x, steps, eta, logits, post_every=25):
    """Draws samples from a model given starting noise."""
    ts = x.new_ones([x.shape[0]])

    # Create the noise schedule
    t = torch.linspace(1, 0, steps + 1)[:-1]
    alphas, sigmas = get_alphas_sigmas(get_crash_schedule(t))

    # The sampling loop
    for i in trange(steps):

        # Get the model output (v, the predicted velocity)
        with torch.cuda.amp.autocast():
            v = model(x, ts * t[i], logits).float()

        # Predict the noise and the denoised image
        pred = x * alphas[i] - v * sigmas[i]
        eps = x * sigmas[i] + v * alphas[i]

        if 0 == i % post_every: # share intermediate results along the way
            # can't get the "plot" part of "plot and hear" to work right
            display(ipd.Audio(rearrange(pred, 'b d n -> d (b n)').cpu(), rate=44100))

        # If we are not on the last timestep, compute the noisy image for the
        # next timestep.
        if i < steps - 1:
            # If eta > 0, adjust the scaling factor for the predicted noise
            # downward according to the amount of additional noise to add
            ddim_sigma = eta * (sigmas[i + 1]**2 / sigmas[i]**2).sqrt() * \
                (1 - alphas[i]**2 / alphas[i + 1]**2).sqrt()
            adjusted_sigma = (sigmas[i + 1]**2 - ddim_sigma**2).sqrt()

            # Recombine the predicted noise and predicted denoised image in the
            # correct proportions for the next step
            x = pred * alphas[i + 1] + eps * adjusted_sigma

            # Add the correct amount of fresh noise
            if eta:
                x += torch.randn_like(x) * ddim_sigma

    # If we are on the last timestep, output the denoised image
    return pred


def make_eps_model_fn(model):
    def eps_model_fn(x, t, **extra_args):
        alphas, sigmas = utils.t_to_alpha_sigma(t)
        v = model(x, t, **extra_args)
        eps = x * sigmas[:, None, None, None] + v * alphas[:, None, None, None]
        return eps
    return eps_model_fn


def make_autocast_model_fn(model, enabled=True):
    def autocast_model_fn(*args, **kwargs):
        with torch.cuda.amp.autocast(enabled):
            return model(*args, **kwargs).float()
    return autocast_model_fn


def transfer(x, eps, t_1, t_2):
    alphas, sigmas = utils.t_to_alpha_sigma(t_1)
    next_alphas, next_sigmas = utils.t_to_alpha_sigma(t_2)
    pred = (x - eps * sigmas[:, None, None, None]) / alphas[:, None, None, None]
    x = pred * next_alphas[:, None, None, None] + eps * next_sigmas[:, None, None, None]
    return x, pred


def prk_step(model, x, t_1, t_2, extra_args):
    eps_model_fn = make_eps_model_fn(model)
    t_mid = (t_2 + t_1) / 2
    eps_1 = eps_model_fn(x, t_1, **extra_args)
    x_1, _ = transfer(x, eps_1, t_1, t_mid)
    eps_2 = eps_model_fn(x_1, t_mid, **extra_args)
    x_2, _ = transfer(x, eps_2, t_1, t_mid)
    eps_3 = eps_model_fn(x_2, t_mid, **extra_args)
    x_3, _ = transfer(x, eps_3, t_1, t_2)
    eps_4 = eps_model_fn(x_3, t_2, **extra_args)
    eps_prime = (eps_1 + 2 * eps_2 + 2 * eps_3 + eps_4) / 6
    x_new, pred = transfer(x, eps_prime, t_1, t_2)
    return x_new, eps_prime, pred


def plms_step(model, x, old_eps, t_1, t_2, extra_args):
    eps_model_fn = make_eps_model_fn(model)
    eps = eps_model_fn(x, t_1, **extra_args)
    eps_prime = (55 * eps - 59 * old_eps[-1] + 37 * old_eps[-2] - 9 * old_eps[-3]) / 24
    x_new, _ = transfer(x, eps_prime, t_1, t_2)
    _, pred = transfer(x, eps, t_1, t_2)
    return x_new, eps, pred


@torch.no_grad()
def prk_sample(model, x, steps, extra_args, is_reverse=False, callback=None):
    """Draws samples from a model given starting noise using fourth-order
    Pseudo Runge-Kutta."""
    ts = x.new_ones([x.shape[0]])
    model_fn = make_autocast_model_fn(model)
    if not is_reverse:
        steps = torch.cat([steps, steps.new_zeros([1])])
    for i in trange(len(steps) - 1, disable=None):
        x, _, pred = prk_step(model_fn, x, steps[i] * ts, steps[i + 1] * ts, extra_args)
        if callback is not None:
            callback({'x': x, 'i': i, 't': steps[i], 'pred': pred})
    return x


@torch.no_grad()
def plms_sample(model, x, steps, extra_args, is_reverse=False, callback=None):
    """Draws samples from a model given starting noise using fourth order
    Pseudo Linear Multistep."""
    ts = x.new_ones([x.shape[0]])
    model_fn = make_autocast_model_fn(model)
    if not is_reverse:
        steps = torch.cat([steps, steps.new_zeros([1])])
    old_eps = []
    for i in trange(len(steps) - 1, disable=None):
        if len(old_eps) < 3:
            x, eps, pred = prk_step(model_fn, x, steps[i] * ts, steps[i + 1] * ts, extra_args)
        else:
            x, eps, pred = plms_step(model_fn, x, old_eps, steps[i] * ts, steps[i + 1] * ts, extra_args)
            old_eps.pop(0)
        old_eps.append(eps)
        if callback is not None:
            callback({'x': x, 'i': i, 't': steps[i], 'pred': pred})
    return x


def pie_step(model, x, t_1, t_2, extra_args):
    eps_model_fn = make_eps_model_fn(model)
    eps_1 = eps_model_fn(x, t_1, **extra_args)
    x_1, _ = transfer(x, eps_1, t_1, t_2)
    eps_2 = eps_model_fn(x_1, t_2, **extra_args)
    eps_prime = (eps_1 + eps_2) / 2
    x_new, pred = transfer(x, eps_prime, t_1, t_2)
    return x_new, eps_prime, pred


def plms2_step(model, x, old_eps, t_1, t_2, extra_args):
    eps_model_fn = make_eps_model_fn(model)
    eps = eps_model_fn(x, t_1, **extra_args)
    eps_prime = (3 * eps - old_eps[-1]) / 2
    x_new, _ = transfer(x, eps_prime, t_1, t_2)
    _, pred = transfer(x, eps, t_1, t_2)
    return x_new, eps, pred


@torch.no_grad()
def pie_sample(model, x, steps, extra_args, is_reverse=False, callback=None):
    """Draws samples from a model given starting noise using second-order
    Pseudo Improved Euler."""
    ts = x.new_ones([x.shape[0]])
    model_fn = make_autocast_model_fn(model)
    if not is_reverse:
        steps = torch.cat([steps, steps.new_zeros([1])])
    for i in trange(len(steps) - 1, disable=None):
        x, _, pred = pie_step(model_fn, x, steps[i] * ts, steps[i + 1] * ts, extra_args)
        if callback is not None:
            callback({'x': x, 'i': i, 't': steps[i], 'pred': pred})
    return x


@torch.no_grad()
def plms2_sample(model, x, steps, extra_args, is_reverse=False, callback=None):
    """Draws samples from a model given starting noise using second order
    Pseudo Linear Multistep."""
    ts = x.new_ones([x.shape[0]])
    model_fn = make_autocast_model_fn(model)
    if not is_reverse:
        steps = torch.cat([steps, steps.new_zeros([1])])
    old_eps = []
    for i in trange(len(steps) - 1, disable=None):
        if len(old_eps) < 1:
            x, eps, pred = pie_step(model_fn, x, steps[i] * ts, steps[i + 1] * ts, extra_args)
        else:
            x, eps, pred = plms2_step(model_fn, x, old_eps, steps[i] * ts, steps[i + 1] * ts, extra_args)
            old_eps.pop(0)
        old_eps.append(eps)
        if callback is not None:
            callback({'x': x, 'i': i, 't': steps[i], 'pred': pred})
    return x



def make_cond_model_fn(model, cond):
  def cond_model_fn(x, t, **extra_args):
    print(x.shape)
    print(t.shape)
    return model(x, t, cond, **extra_args)
  return cond_model_fn


def demo(model, log_dict, zsum, zmix, demo_samples, demo_steps=250, sr=48000):
    demo_batch_size=zsum.shape[0]

    noise = torch.randn([demo_batch_size, 2, demo_samples]).to(model.device)
    model_fn = make_cond_model_fn(model.diffusion_ema, zsum0)

    # Run the sampler
    fakes = sample(model.diffusion_ema, noise, 500, 1, zsum)
    fakes = rearrange(fakes, 'b d n -> d (b n)')
    log_dict['zsum'] = wandb.Audio(filename, sample_rate=sr, caption='zsum')
    fakes = sample(model.diffusion_ema, noise, 500, 1, zsum)
    fakes = rearrange(fakes, 'b d n -> d (b n)')
    log_dict['zsum'] = wandb.Audio(filename, sample_rate=sr, caption='zsum')
    return log_dict


# Cell
def get_stems_faders(batch, dl, maxstems=6):
    "grab some more audio stems and set faders"
    nstems = 1 + int(torch.randint(maxstems-1,(1,1))[0][0].numpy()) # an int between 1 and maxstems, PyTorch style :-/
    faders = 2*torch.rand(nstems)-1  # fader gains can be from -1 to 1
    stems = [batch]
    dl_iter = iter(dl)
    for i in range(nstems-1):
        stems.append(next(dl_iter)[0])  # [0] is because there are two items returned and audio is the first
    return stems, faders

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
    hprint = HostPrinter(accelerator)
    hprint(f'Using device: {device}')

    encoder_choices = ['ad','icebox']
    encoder_choice = encoder_choices[0]
    hprint(f"Using {encoder_choice} as encoder")
    if 'icebox' == encoder_choice:
        args.latent_dim = 64  # overwrite latent_dim with what Jukebox requires
        encoder = IceBoxEncoder(args, device)
    elif 'ad' == encoder_choice:
        dvae = DiffusionDVAE(args, device)
        #dvae = setup_weights(dvae, accelerator, device)
        #encoder = dvae.encoder
        #freeze(dvae)

    hprint("Setting up AA model")
    aa_model = AudioAlgebra(args, device, dvae)

    hprint(f'  AA Model Parameters: {n_params(aa_model)}')

    # If logging to wandb, initialize the run
    use_wandb = accelerator.is_main_process and args.name
    if use_wandb:
        import wandb
        config = vars(args)
        config['params'] = n_params(aa_model)
        wandb.init(project=args.name, config=config, save_code=True)

    opt = optim.Adam([*aa_model.reembedding.parameters()], lr=4e-5)

    hprint("Setting up dataset")
    train_set = MultiStemDataset([args.training_dir], args)
    train_dl = torchdata.DataLoader(train_set, args.batch_size, shuffle=True,
                               num_workers=args.num_workers, persistent_workers=True, pin_memory=True)

    hprint("Calling accelerator.prepare")
    aa_model, opt, train_dl, dvae = accelerator.prepare(aa_model, opt, train_dl, dvae)

    hprint("Setting up frozen encoder model weights")
    dvae = setup_weights(dvae, accelerator)
    freeze(accelerator.unwrap_model(dvae))
    #encoder = dvae.encoder

    hprint("Setting up wandb")
    if use_wandb:
        wandb.watch(aa_model)

    hprint("Checking for checkpoint")
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
    hprint("Let's go...")
    try:
        while True:  # training loop
            #print(f"Starting epoch {epoch}")
            for batch in tqdm(train_dl, disable=not accelerator.is_main_process):
                batch = batch[0]  # first elem is the audio, 2nd is the filename which we don't need
                #if accelerator.is_main_process: print(f"e{epoch} s{step}: got batch. batch.shape = {batch.shape}")
                opt.zero_grad()

                # "batch" is actually not going to have all the data we want. We could rewrite the dataloader to fix this,
                # but instead I just added get_stems_faders() which grabs "even more" audio to go with "batch"
                stems, faders = get_stems_faders(batch, train_dl)

                zsum, zmix, zarchive = accelerator.unwrap_model(aa_model).forward(stems,faders)
                loss = accelerator.unwrap_model(aa_model).loss(zsum, zmix, zarchive)
                accelerator.backward(loss)
                opt.step()

                if accelerator.is_main_process:
                    if step % 25 == 0:
                        tqdm.write(f'Epoch: {epoch}, step: {step}, loss: {loss.item():g}')

                    if use_wandb:
                        log_dict = {
                            'epoch': epoch,
                            'loss': loss.item(),
                            #'lr': sched.get_last_lr()[0],
                            'zsum_pca': pca_point_cloud(zsum.detach()),
                            'zmix_pca': pca_point_cloud(zmix.detach())
                        }

                    if step % args.demo_every == 0:
                        log_dict = demo(aa_model, log_dict, zsum, zmix, batch.shape[1])

                    if use_wandb: wandb.log(log_dict, step=step)

                if step > 0 and step % args.checkpoint_every == 0:
                    save(accelerator, args, aa_model, opt, epoch, step)

                step += 1
            epoch += 1
    except RuntimeError as err:  # ??
        import requests
        import datetime
        ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        resp = requests.get('http://169.254.169.254/latest/meta-data/instance-id')
        hprint(f'ERROR at {ts} on {resp.text} {device}: {type(err).__name__}: {err}', flush=True)
        raise err
    except KeyboardInterrupt:
        pass

# Cell
# Not needed if listed in console_scripts in settings.ini
if __name__ == '__main__' and "get_ipython" not in dir():  # don't execute in notebook
    main()