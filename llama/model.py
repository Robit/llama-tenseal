# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the GNU General Public License version 3.

from typing import Optional, Tuple
from dataclasses import dataclass
import math

import torch
from torch import nn
import torch.nn.functional as F

import tenseal as ts
import time

context = ts.context(
            ts.SCHEME_TYPE.CKKS,
            poly_modulus_degree = 8192,
            coeff_mod_bit_sizes = [20, 20, 20, 20, 20, 21, 21, 21, 21, 21]
        )
context.generate_galois_keys()
context.global_scale = 2**20

@dataclass
class ModelArgs:
    dim: int = 512
    n_layers: int = 8
    n_heads: int = 8
    vocab_size: int = -1  # defined later by tokenizer
    multiple_of: int = 256  # make SwiGLU hidden layer size multiple of large power of 2
    norm_eps: float = 1e-5

    max_batch_size: int = 32
    max_seq_len: int = 1024


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)  # type: ignore
    freqs = torch.outer(t, freqs).float()  # type: ignore
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return freqs_cis


def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 < ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i == 1 or i == ndim - 1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)


def apply_rotary_emb(
        xq: torch.Tensor,
        xk: torch.Tensor,
        n_local_heads, head_dim, seqlen,
        freqs_cis: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    ll = torch.view_as_real(freqs_cis)[:, :, 0:1]
    freqs_cis_l = torch.cat((ll, ll), dim=-1).flatten(1).view(seqlen, 1, -1)  # shape: (seq_len, 1, 128)
    rr = torch.view_as_real(freqs_cis)[:, :, 1:2]  # shape: (seq_len, 64, 1)
    freqs_cis_r = torch.cat((-rr, rr), dim=-1).flatten(1).view(seqlen, 1, -1)  # shape: (seq_len, 1, 128)
    xq_ = xq.view(seqlen, n_local_heads, head_dim//2, 2)[:, :, :, [1, 0]].flatten(2)
    xq = xq * freqs_cis_l + xq_ * freqs_cis_r
    xk_ = xk.view(seqlen, n_local_heads, head_dim//2, 2)[:, :, :, [1, 0]].flatten(2)
    xk = xk * freqs_cis_l + xk_ * freqs_cis_r
    return xq, xk
    """
    #"""
    #start_time = time.time()

    #xq = ts.ckks_tensor(context, xq)
    #xk = ts.ckks_tensor(context, xk)

    #print(f"Time in enc {time.time() - start_time:.2f}") #6.04
    
    #start_time = time.time()

    ll = torch.view_as_real(freqs_cis)[:, :, 0:1]
    freqs_cis_l = torch.cat((ll, ll), dim=-1).flatten(1).view(seqlen, 1, -1)
    rr = torch.view_as_real(freqs_cis)[:, :, 1:2]
    freqs_cis_r = torch.cat((-rr, rr), dim=-1).flatten(1).view(seqlen, 1, -1)
    xq_ = xq.reshape([seqlen, n_local_heads, head_dim//2, 2])[:, :, :, slice(0, 2)].reshape([seqlen, n_local_heads, head_dim]) # 1,0 -> 0,1 .flatten(2) -> .reshape([seqlen, n_local_heads, head_dim])
    xq = xq * freqs_cis_l + xq_ * freqs_cis_r
    xk_ = xk.reshape([seqlen, n_local_heads, head_dim//2, 2])[:, :, :, slice(0, 2)].reshape([seqlen, n_local_heads, head_dim]) # 1,0 -> 0,1 .flatten(2) -> .reshape([seqlen, n_local_heads, head_dim])
    xk = xk * freqs_cis_l + xk_ * freqs_cis_r

    #print(f"Time in op {time.time() - start_time:.2f}") #21.64
 
    #start_time = time.time()

    #print(f"Time in dec {time.time() - start_time:.2f}") #10.17
    
    return xq, xk
    #"""

def plainToTorch(plain):
    return torch.tensor(plain.raw[0 : math.prod(plain.shape)], dtype=torch.float32).reshape(plain.shape)

def encryptedLinearTransform(linear, tensor):
    tensor = tensor.decrypt()
    return ts.ckks_tensor(context, linear(plainToTorch(tensor)))

class Attention(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()

        self.n_local_heads = args.n_heads
        self.head_dim = args.dim // args.n_heads

        self.wq = nn.Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False,
        )

        self.wk = nn.Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False,
        )

        self.wv = nn.Linear(
            args.dim,
            args.n_heads * self.head_dim,
            bias=False,
        )

        self.wo = nn.Linear(
            args.n_heads * self.head_dim,
            args.dim,
            bias=False,
        )

        self.cache_k = torch.zeros(
            (args.max_batch_size, args.max_seq_len, self.n_local_heads, self.head_dim)
        ).cpu()
        self.cache_v = torch.zeros(
            (args.max_batch_size, args.max_seq_len, self.n_local_heads, self.head_dim)
        ).cpu()

    def forward(self, x: torch.Tensor, start_pos: int, freqs_cis: torch.Tensor, mask: Optional[torch.Tensor]):
        bsz, seqlen, _ = x.shape

        x = ts.ckks_tensor(context, x.squeeze())
        
        start_time = time.time()
        xq = encryptedLinearTransform(self.wq, x)
        print("xq transform done")

        xk = encryptedLinearTransform(self.wk, x)
        print("xk transform done")

        xv = encryptedLinearTransform(self.wv, x)
        print("transforms done")
        print(f"Time: {time.time() - start_time:.2f}")

        start_time = time.time()
        xq = xq.reshape([seqlen, self.n_local_heads, self.head_dim])
        print("reshape1 done")
        xk = xk.reshape([seqlen, self.n_local_heads, self.head_dim])
        print("reshape2 done")
        xv = xv.reshape([seqlen, self.n_local_heads, self.head_dim])
        print("reshape3 done")
        print(f"Time: {time.time() - start_time:.2f}")

        start_time = time.time()
        xq, xk = apply_rotary_emb(xq, xk, self.n_local_heads, self.head_dim, seqlen, freqs_cis=freqs_cis)
        print("apply_rotary_embedding done")
        print(f"Time: {time.time() - start_time:.2f}")

        start_time = time.time()
        xq, xk, xv = plainToTorch(xq.decrypt()), plainToTorch(xk.decrypt()), plainToTorch(xv.decrypt())
        print("decrypt done")
        print(f"Time: {time.time() - start_time:.2f}")

        self.cache_k = self.cache_k.to(xq)
        self.cache_v = self.cache_v.to(xq)

        self.cache_k[:bsz, start_pos: start_pos + seqlen] = xk
        self.cache_v[:bsz, start_pos: start_pos + seqlen] = xv

        keys = self.cache_k[:bsz, : start_pos + seqlen]
        values = self.cache_v[:bsz, : start_pos + seqlen]

        xq = xq.transpose(0, 1)
        keys = keys.transpose(1, 2)
        values = values.transpose(1, 2)
        scores = torch.matmul(xq, keys.transpose(2, 3)) / math.sqrt(self.head_dim)
        if mask is not None:
            scores = scores + mask  # (bs, n_local_heads, slen, cache_len + slen)
        scores = F.softmax(scores.float(), dim=-1).type_as(xq)
        output = torch.matmul(scores, values)  # (bs, n_local_heads, slen, head_dim)
        output = output.transpose(
            1, 2
        ).contiguous().view(bsz, seqlen, -1)

        return self.wo(output)


class FeedForward(nn.Module):
    def __init__(
            self,
            dim: int,
            hidden_dim: int,
            multiple_of: int,
    ):
        super().__init__()
        hidden_dim = int(2 * hidden_dim / 3)
        hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.w1 = nn.Linear(
            dim, hidden_dim, bias=False,
        )
        self.w2 = nn.Linear(
            hidden_dim, dim, bias=False,
        )
        self.w3 = nn.Linear(
            dim, hidden_dim, bias=False,
        )

    def forward(self, x):
        return self.w2(F.silu(self.w1(x)) * self.w3(x))


class TransformerBlock(nn.Module):
    def __init__(self, layer_id: int, args: ModelArgs):
        super().__init__()
        self.n_heads = args.n_heads
        self.dim = args.dim
        self.head_dim = args.dim // args.n_heads
        self.attention = Attention(args)
        self.feed_forward = FeedForward(
            dim=args.dim, hidden_dim=4 * args.dim, multiple_of=args.multiple_of
        )
        self.layer_id = layer_id
        self.attention_norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.ffn_norm = RMSNorm(args.dim, eps=args.norm_eps)

    def forward(self, x: torch.Tensor, start_pos: int, freqs_cis: torch.Tensor, mask: Optional[torch.Tensor]):
        h = x + self.attention.forward(self.attention_norm(x), start_pos, freqs_cis, mask)
        out = h + self.feed_forward.forward(self.ffn_norm(h))
        return out


class Transformer(nn.Module):
    def __init__(self, params: ModelArgs):
        super().__init__()
        self.params = params
        self.vocab_size = params.vocab_size
        self.n_layers = params.n_layers

        self.tok_embeddings = nn.Embedding(
            params.vocab_size, params.dim
        )

        self.layers = torch.nn.ModuleList()
        for layer_id in range(params.n_layers):
            self.layers.append(TransformerBlock(layer_id, params))

        self.norm = RMSNorm(params.dim, eps=params.norm_eps)

        self.output = nn.Linear(
            params.dim, params.vocab_size, bias=False,
        )

        self.freqs_cis = precompute_freqs_cis(
            self.params.dim // self.params.n_heads, self.params.max_seq_len * 2
        )

    @torch.inference_mode()
    def forward(self, tokens: torch.Tensor, start_pos: int):
        _bsz, seqlen = tokens.shape
        h = self.tok_embeddings(tokens)
        self.freqs_cis = self.freqs_cis.to(h.device)
        freqs_cis = self.freqs_cis[start_pos: start_pos + seqlen]

        mask = None
        if seqlen > 1:
            mask = torch.full((1, 1, seqlen, seqlen), float("-inf"), device=tokens.device)
            mask = torch.triu(mask, diagonal=start_pos + 1).type_as(h)

        for layer in self.layers:
            h = layer(h, start_pos, freqs_cis, mask)
        h = self.norm(h)
        output = self.output(h[:, -1, :])  # only compute last logits
        return output.float()
