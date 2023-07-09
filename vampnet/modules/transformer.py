import logging
import math
from typing import Optional
from typing import Tuple
from typing import Union

import audiotools as at
import loralib as lora
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from x_transformers import ContinuousTransformerWrapper
from x_transformers import Encoder

from ..mask import _gamma, scalar_to_batch_tensor
from ..util import codebook_flatten
from ..util import codebook_unflatten
from .condition import ChromaStemConditioner
from .layers import CodebookEmbedding
from .layers import WNConv1d

LORA_R = 8


def gumbel_noise_like(t):
    noise = torch.zeros_like(t).uniform_(1e-20, 1)
    return -torch.log(-torch.log(noise))


def gumbel_sample(t, temperature=1.0, dim=-1):
    return ((t / max(temperature, 1e-10)) + gumbel_noise_like(t)).argmax(dim=dim)


class VampNet(at.ml.BaseModel):
    def __init__(
        self,
        n_heads: int = 20,
        n_layers: int = 16,
        n_codebooks: int = 9,
        n_conditioning_codebooks: int = 0,
        latent_dim: int = 8,
        embedding_dim: int = 1280,
        vocab_size: int = 1024,
        dropout: float = 0.1,
        chroma_dim: int = 0,
        max_seq_len: int = 1024,
    ):
        super().__init__()
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.n_codebooks = n_codebooks
        self.n_conditioning_codebooks = n_conditioning_codebooks
        self.embedding_dim = embedding_dim
        self.vocab_size = vocab_size
        self.latent_dim = latent_dim
        self.max_seq_len = max_seq_len

        self.chroma_dim = chroma_dim

        self.embedding = CodebookEmbedding(
            latent_dim=latent_dim,
            n_codebooks=n_codebooks,
            vocab_size=vocab_size,
            emb_dim=embedding_dim,
            special_tokens=["MASK"],
        )
        self.mask_token = self.embedding.special_idxs["MASK"]

        self.lm = ContinuousTransformerWrapper(
            max_seq_len=max_seq_len,
            attn_layers=Encoder(
                dim=self.embedding_dim,
                depth=self.n_layers,
                heads=self.n_heads,
                attn_flash=True,
            ),
            emb_dropout=dropout,
        )

        # Add final conv layer
        self.n_predict_codebooks = n_codebooks - n_conditioning_codebooks
        self.classifier = nn.Sequential(
            WNConv1d(
                embedding_dim,
                vocab_size * self.n_predict_codebooks,
                kernel_size=1,
                padding="same",
                # groups=self.n_predict_codebooks,
            ),
        )

        if self.chroma_dim > 0:
            self.chroma_embedding = nn.Embedding(self.chroma_dim, self.embedding_dim)

    def forward(self, x, chroma=None, chroma_dropout: float = 0.2):
        x = self.embedding(x)
        x_mask = torch.ones_like(x, dtype=torch.bool)[:, :1, :].squeeze(1)

        if self.chroma_dim > 0:
            assert chroma is not None
            chroma = self.chroma_embedding(chroma)

            # apply a chroma mask on the batch dimension
            chroma_mask = torch.rand(chroma.shape[0]) > chroma_dropout
            chroma_mask = chroma_mask.unsqueeze(-1).unsqueeze(-1).to(chroma.device)
            chroma = chroma * chroma_mask

            x = x + chroma

        x = rearrange(x, "b d n -> b n d")
        out = self.lm(x)
        out = rearrange(out, "b n d -> b d n")

        out = self.classifier(out)
        out = rearrange(out, "b (p c) t -> b p (t c)", c=self.n_predict_codebooks)

        return out

    @torch.no_grad()
    def to_signal(self, z, codec):
        """
        convert a sequence of latents to a signal.
        """
        assert z.ndim == 3

        signal = at.AudioSignal(
            codec.decode(
                codec.quantizer.from_latents(self.embedding.from_codes(z, codec))[0]
            )["audio"],
            codec.sample_rate,
        )

        # find where the mask token is and replace it with silence in the audio
        for tstep in range(z.shape[-1]):
            if torch.any(z[:, :, tstep] == self.mask_token):
                sample_idx_0 = tstep * codec.hop_length
                sample_idx_1 = sample_idx_0 + codec.hop_length
                signal.samples[:, :, sample_idx_0:sample_idx_1] = 0.0

        return signal

    @torch.no_grad()
    def generate(
        self,
        codec,
        time_steps: int = 300,
        sampling_steps: int = 24,
        start_tokens: Optional[torch.Tensor] = None,
        mask: Optional[torch.Tensor] = None,
        temperature: Union[float, Tuple[float, float]] = 8.0,
        typical_filtering=False,
        typical_mass=0.2,
        typical_min_tokens=1,
        return_signal=True,
    ):
        # TODO: need to take chroma in as input, and then implement classifier free guidance
        logging.debug(f"beginning generation with {sampling_steps} steps")

        #####################
        # resolve temperature #
        #####################
        assert isinstance(temperature, float)
        logging.debug(f"temperature: {temperature}")

        #####################
        # resolve initial z #
        #####################
        z = start_tokens

        if z is None:
            z = torch.full((1, self.n_codebooks, time_steps), self.mask_token).to(
                self.device
            )

        logging.debug(f"created z with shape {z.shape}")

        #################
        # resolve mask #
        #################

        if mask is None:
            mask = torch.ones_like(z).to(self.device).int()
            mask[:, : self.n_conditioning_codebooks, :] = 0.0
        if mask.ndim == 2:
            mask = mask[:, None, :].repeat(1, z.shape[1], 1)
        logging.debug(f"created mask with shape {mask.shape}")

        ###########
        # set up #
        ##########
        # apply the mask to z
        z_masked = z.masked_fill(mask.bool(), self.mask_token)
        # logging.debug(f"z_masked: {z_masked}")

        # how many mask tokens to begin with?
        num_mask_tokens_at_start = (z_masked == self.mask_token).sum()
        logging.debug(f"num mask tokens at start: {num_mask_tokens_at_start}")

        # how many codebooks are we inferring vs conditioning on?
        n_infer_codebooks = self.n_codebooks - self.n_conditioning_codebooks
        logging.debug(f"n infer codebooks: {n_infer_codebooks}")

        #################
        # begin sampling #
        #################

        for i in range(sampling_steps):
            logging.debug(f"step {i} of {sampling_steps}")

            # our current temperature
            logging.debug(f"temperature: {temperature}")

            # our current schedule step
            r = scalar_to_batch_tensor(
                (i + 1) / sampling_steps, 
                z.shape[0]
            ).to(z.device)
            logging.debug(f"r: {r}")

            # get latents
            latents = self.embedding.from_codes(z_masked, codec)
            logging.debug(f"computed latents with shape: {latents.shape}")

            # infer from latents
            # NOTE: this collapses the codebook dimension into the sequence dimension
            logits = self.forward(latents, chroma=None)  # b, prob, seq
            logits = logits.permute(0, 2, 1)  # b, seq, prob
            if typical_filtering:
                typical_filter(logits, 
                               typical_mass=typical_mass, 
                               typical_min_tokens=typical_min_tokens
                )

            logging.debug(f"permuted logits with shape: {logits.shape}")

            # logits2probs
            probs = torch.softmax(logits, dim=-1)
            logging.debug(f"computed probs with shape: {probs.shape}")

            # sample from logits with multinomial sampling
            b = probs.shape[0]
            probs = rearrange(probs, "b seq prob -> (b seq) prob")

            sampled_z = torch.multinomial(probs, 1).squeeze(-1)

            sampled_z = rearrange(sampled_z, "(b seq)-> b seq", b=b)
            probs = rearrange(probs, "(b seq) prob -> b seq prob", b=b)
            logging.debug(f"sampled z with shape: {sampled_z.shape}")

            # get the confidences: which tokens did we sample? 
            selected_probs = (
                torch.take_along_dim(
                    probs, sampled_z.long().unsqueeze(-1), 
                    dim=-1
                ).squeeze(-1)
            )

            # flatten z_masked and mask, so we can deal with the sampling logic
            # we'll unflatten them at the end of the loop for the next forward pass
            # remove conditioning codebooks, we'll add them back at the end
            z_masked = codebook_flatten(z_masked[:, self.n_conditioning_codebooks:, :])           

            mask = (z_masked == self.mask_token).int()
            
            # update the mask, remove conditioning codebooks from the mask
            logging.debug(f"updated mask with shape: {mask.shape}")
            # add z back into sampled z where the mask was false
            sampled_z = torch.where(
                mask.bool(), sampled_z, z_masked
            )
            logging.debug(f"added z back into sampled z with shape: {sampled_z.shape}")

            # ignore any tokens that weren't masked
            selected_probs = torch.where(
                mask.bool(), selected_probs, torch.inf
            )

            # get the num tokens to mask, according to the schedule
            num_to_mask = torch.floor(_gamma(r) * num_mask_tokens_at_start).unsqueeze(1).long()
            logging.debug(f"num to mask: {num_to_mask}")

            if i != (sampling_steps - 1):
                num_to_mask = torch.maximum(
                    torch.tensor(1),
                    torch.minimum(
                        mask.sum(dim=-1, keepdim=True) - 1,
                        num_to_mask
                    )
                )

            # get our new mask
            mask = mask_by_random_topk(
                num_to_mask, selected_probs, temperature * (1-r)
            )  

            # update the mask
            z_masked = torch.where(
                mask.bool(), self.mask_token, sampled_z
            )
            logging.debug(f"updated z_masked with shape: {z_masked.shape}")

            z_masked = codebook_unflatten(z_masked, n_infer_codebooks)
            mask = codebook_unflatten(mask, n_infer_codebooks)
            logging.debug(f"unflattened z_masked with shape: {z_masked.shape}")

            # add conditioning codebooks back to z_masked
            z_masked = torch.cat(
                (z[:, :self.n_conditioning_codebooks, :], z_masked), dim=1
            )
            logging.debug(f"added conditioning codebooks back to z_masked with shape: {z_masked.shape}")


        # add conditioning codebooks back to sampled_z
        sampled_z = codebook_unflatten(sampled_z, n_infer_codebooks)
        sampled_z = torch.cat(
            (z[:, :self.n_conditioning_codebooks, :], sampled_z), dim=1
        )

        logging.debug(f"finished sampling")

        if return_signal:
            return self.to_signal(sampled_z, codec)
        else:
            return sampled_z

def mask_by_random_topk(
    num_to_mask: int, probs: torch.Tensor, temperature: float = 1.0
):
    """
    Args:
        num_to_mask (int): number of tokens to mask
        probs (torch.Tensor): probabilities for each sampled event, shape (batch, seq)
        temperature (float, optional): temperature. Defaults to 1.0.
    """
    logging.debug(f"masking by random topk")
    logging.debug(f"num to mask: {num_to_mask}")
    logging.debug(f"probs shape: {probs.shape}")
    logging.debug(f"temperature: {temperature}")
    logging.debug("")

    confidence = torch.log(probs) + temperature * gumbel_noise_like(probs)
    logging.debug(f"confidence shape: {confidence.shape}")

    sorted_confidence, sorted_idx = confidence.sort(dim=-1)
    logging.debug(f"sorted confidence shape: {sorted_confidence.shape}")
    logging.debug(f"sorted idx shape: {sorted_idx.shape}")

    # get the cut off threshold, given the mask length
    cut_off = torch.take_along_dim(sorted_confidence, num_to_mask, axis=-1)
    logging.debug(f"cut off shape: {cut_off.shape}")

    # mask out the tokens
    mask = confidence < cut_off
    logging.debug(f"mask shape: {mask.shape}")

    return mask


def typical_filter(
    logits,
    typical_mass: float = 0.95,
    typical_min_tokens: int = 1,
):
    nb, nt, _ = logits.shape
    x_flat = rearrange(logits, "b t l -> (b t ) l")
    x_flat_norm = torch.nn.functional.log_softmax(x_flat, dim=-1)
    x_flat_norm_p = torch.exp(x_flat_norm)
    entropy = -(x_flat_norm * x_flat_norm_p).nansum(-1, keepdim=True)

    c_flat_shifted = torch.abs((-x_flat_norm) - entropy)
    c_flat_sorted, x_flat_indices = torch.sort(c_flat_shifted, descending=False)
    x_flat_cumsum = x_flat.gather(-1, x_flat_indices).softmax(dim=-1).cumsum(dim=-1)

    last_ind = (x_flat_cumsum < typical_mass).sum(dim=-1)
    sorted_indices_to_remove = c_flat_sorted > c_flat_sorted.gather(
        1, last_ind.view(-1, 1)
    )
    if typical_min_tokens > 1:
        sorted_indices_to_remove[..., :typical_min_tokens] = 0
    indices_to_remove = sorted_indices_to_remove.scatter(
        1, x_flat_indices, sorted_indices_to_remove
    )
    x_flat = x_flat.masked_fill(indices_to_remove, -float("Inf"))
    logits = rearrange(x_flat, "(b t) l -> b t l", t=nt)
    return logits


if __name__ == "__main__":
    # import argbind
    from .layers import num_params

    VampNet = argbind.bind(VampNet)

    @argbind.bind(without_prefix=True)
    def try_model(device: str = "cuda", batch_size: int = 2, seq_len_s: float = 10.0):
        seq_len = int(32000 / 512 * seq_len_s)

        model = VampNet().to(device)

        z = torch.randint(
            0, model.vocab_size, size=(batch_size, model.n_codebooks, seq_len)
        ).to(device)

        r = torch.zeros(batch_size).to(device)

        z_mask_latent = torch.rand(
            batch_size, model.latent_dim * model.n_codebooks, seq_len
        ).to(device)
        z_hat = model(z_mask_latent, r)

        pred = z_hat.argmax(dim=1)
        pred = model.embedding.unflatten(pred, n_codebooks=model.n_predict_codebooks)

        print(f"model has {num_params(model)/1e6:<.3f}M parameters")
        print(f"prediction has shape {pred.shape}")
        breakpoint()

    args = argbind.parse_args()
    with argbind.scope(args):
        try_model()
