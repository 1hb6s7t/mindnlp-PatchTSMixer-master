# coding=utf-8
# Copyright 2024 Microsoft Research and HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""MindSpore UDOP model."""

import collections
import math
import random
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple, Union

import mindspore
from mindspore import Tensor
from mindnlp.core import nn, ops
from mindnlp.core.nn import CrossEntropyLoss

from .configuration_udop import UdopConfig
from ...modeling_outputs import (
    Seq2SeqLMOutput,
    Seq2SeqModelOutput,
)

from ....common.activations import ACT2FN
from ...modeling_utils import PreTrainedModel
from ...ms_utils import find_pruneable_heads_and_indices, prune_linear_layer
from ....utils import (
    ModelOutput,
    logging,
)


logger = logging.get_logger(__name__)


_CONFIG_FOR_DOC = "UdopConfig"


@dataclass
class BaseModelOutputWithAttentionMask(ModelOutput):
    """
    Class for the model's outputs that may also contain a past key/values (to speed up sequential decoding). Includes
    an additional attention mask.

    Args:
        last_hidden_state (`mindspore.Tensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model. If `past_key_values` is used only
            the last hidden-state of the sequences of shape `(batch_size, 1, hidden_size)` is output.
        past_key_values (`tuple(tuple(mindspore.Tensor))`, *optional*, returned when `use_cache=True` is passed or
        when `config.use_cache=True`):
            Tuple of `tuple(mindspore.Tensor)` of length `config.n_layers`, with each tuple having 2 tensors of shape
            `(batch_size, num_heads, sequence_length, embed_size_per_head)`) and optionally if
            `config.is_encoder_decoder=True` 2 additional tensors of shape `(batch_size, num_heads,
            encoder_sequence_length, embed_size_per_head)`. Contains pre-computed hidden-states (key and values in the
            self-attention blocks and optionally if `config.is_encoder_decoder=True` in the cross-attention blocks)
            that can be used (see `past_key_values` input) to speed up sequential decoding.
        hidden_states (`tuple(mindspore.Tensor)`, *optional*, returned when `output_hidden_states=True` is passed or
        when `config.output_hidden_states=True`):
            Tuple of `mindspore.Tensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`. Hidden-states of
            the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(mindspore.Tensor)`, *optional*, returned when `output_attentions=True` is passed or when
        `config.output_attentions=True`):
            Tuple of `mindspore.Tensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`. Attentions weights after the attention softmax, used to compute the weighted average in
            the self-attention heads.
        cross_attentions (`tuple(mindspore.Tensor)`, *optional*, returned when `output_attentions=True` and
        `config.add_cross_attention=True` is passed or when `config.output_attentions=True`):
            Tuple of `mindspore.Tensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`. Attentions weights of the decoder's cross-attention layer, after the attention softmax,
            used to compute the weighted average in the cross-attention heads.
    """

    last_hidden_state: mindspore.Tensor = None
    attention_mask: mindspore.Tensor = None
    past_key_values: Optional[Tuple[Tuple[mindspore.Tensor]]] = None
    hidden_states: Optional[Tuple[mindspore.Tensor]] = None
    attentions: Optional[Tuple[mindspore.Tensor]] = None
    cross_attentions: Optional[Tuple[mindspore.Tensor]] = None


def get_visual_bbox(image_size=224, patch_size=16):
    image_feature_pool_shape = [image_size // patch_size, image_size // patch_size]
    visual_bbox_x = ops.arange(0, 1.0 * (image_feature_pool_shape[1] + 1), 1.0)
    visual_bbox_x /= image_feature_pool_shape[1]

    visual_bbox_y = ops.arange(0, 1.0 * (image_feature_pool_shape[0] + 1), 1.0)
    visual_bbox_y /= image_feature_pool_shape[0]

    visual_bbox_input = ops.stack(
        [
            visual_bbox_x[:-1].tile((image_feature_pool_shape[0], 1)),
            ops.transpose(visual_bbox_y[:-1].tile((image_feature_pool_shape[1], 1)), 0, 1),
            visual_bbox_x[1:].tile((image_feature_pool_shape[0], 1)),
            ops.transpose(visual_bbox_y[1:].tile((image_feature_pool_shape[1], 1)), 0, 1),
        ],
        dim=-1,
    )

    visual_bbox_input = visual_bbox_input.view(-1, 4)

    return visual_bbox_input


def pad_sequence(seq, target_len, pad_value=0):
    if isinstance(seq, mindspore.Tensor):
        n = seq.shape[0]
    else:
        n = len(seq)
        seq = mindspore.tensor(seq)
    m = target_len - n
    if m > 0:
        ret = ops.stack([pad_value] * m).to(seq)
        seq = ops.cat([seq, ret], dim=0)
    return seq[:target_len]


def combine_image_text_embeddings(
    image_embeddings,
    inputs_embeds,
    bbox,
    visual_bbox,
    attention_mask=None,
    num_patches=14,
    max_len=0,
    image_size=224,
    patch_size=16,
):
    """
    Combine the image and text embeddings for the input to the encoder/decoder of UDOP.

    First, the image embeddings are created by checking for each visual patch if it is inside the bounding box of a
    token. If it is, the visual patch is combined with the token embedding. Then, the visual bounding boxes are combined
    with the text bounding boxes. Finally, the visual bounding boxes are combined with the text attention mask.
    """

    sequence_length = num_patches
    ocr_points_x = ops.clip(
        ops.floor((bbox[:, :, 0] + bbox[:, :, 2]) / 2.0 * sequence_length).long(), 0, sequence_length - 1
    )
    ocr_points_y = (
        ops.clip(ops.floor((bbox[:, :, 1] + bbox[:, :, 3]) / 2.0 * sequence_length).long(), 0, sequence_length - 1)
        * sequence_length
    )
    ocr_points = ocr_points_x + ocr_points_y
    # make sure bounding boxes are of type float to calculate means
    bbox = bbox.to(mindspore.float64)
    target_seg = (bbox.mean(-1) == 0.0) | (bbox.mean(-1) == 1.0)
    repeated_vision_embeds = ops.gather(
        image_embeddings, 1, ocr_points.unsqueeze(-1).tile((1, 1, image_embeddings.shape[-1]))
    )
    repeated_vision_embeds[target_seg] = 0.0
    inputs_embeds += repeated_vision_embeds

    patch_inds = ops.full_like(image_embeddings[:, :, 0], True).bool()
    ind = ops.cat(
        [
            ops.arange(len(ocr_points))[:, None].tile((1, ocr_points.shape[-1]))[:, :, None].to(ocr_points),
            ocr_points[:, :, None],
        ],
        dim=-1,
    )
    ind = ind.flatten(0, 1)
    rows, cols = zip(*ind)
    patch_inds[rows, cols] = False

    input_vision_patches = [image_embeddings[i][patch_inds[i]] for i in range(len(patch_inds))]

    if visual_bbox is None:
        visual_bbox = get_visual_bbox(image_size=image_size, patch_size=patch_size)
        visual_bbox = visual_bbox.unsqueeze(0).tile((image_embeddings.shape[0], 1, 1))

    visual_bbox = [visual_bbox[i][patch_inds[i]] for i in range(len(patch_inds))]
    if attention_mask is not None:
        visual_attention_mask = [mindspore.tensor([1] * len(item)).to(attention_mask) for item in visual_bbox]

    if max_len == 0:
        max_len = image_embeddings.shape[1]
    else:
        max_len = max_len - inputs_embeds.shape[1]
    inputs_vision_patches = ops.stack(
        [pad_sequence(item, max_len, ops.zeros_like(image_embeddings[0, 0])) for item in input_vision_patches]
    )
    visual_bbox = ops.stack([pad_sequence(item, max_len, ops.zeros_like(bbox[0, 0])) for item in visual_bbox])
    if attention_mask is not None:
        visual_attention_mask = ops.stack(
            [pad_sequence(item, max_len, ops.zeros_like(attention_mask[0, 0])) for item in visual_attention_mask]
        )

    inputs_embeds = ops.cat([inputs_embeds, inputs_vision_patches], 1)
    bbox = ops.cat([bbox, visual_bbox], 1)
    if attention_mask is not None:
        attention_mask = ops.cat([attention_mask, visual_attention_mask], 1)
    return inputs_embeds, bbox, attention_mask


class UdopPatchEmbeddings(nn.Module):
    """2D Image to Patch Embeddings"""

    def __init__(self, config):
        super().__init__()
        image_size, patch_size = config.image_size, config.patch_size
        num_channels, hidden_size = config.num_channels, config.hidden_size

        image_size = image_size if isinstance(image_size, collections.abc.Iterable) else (image_size, image_size)
        patch_size = patch_size if isinstance(patch_size, collections.abc.Iterable) else (patch_size, patch_size)
        num_patches = (image_size[1] // patch_size[1]) * (image_size[0] // patch_size[0])
        self.image_size = image_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.num_patches = num_patches

        self.proj = nn.Conv2d(num_channels, hidden_size, kernel_size=patch_size, stride=patch_size)

    def forward(self, pixel_values):
        batch_size, num_channels, height, width = pixel_values.shape
        if height != self.image_size[0] or width != self.image_size[1]:
            raise ValueError(
                f"Input image size ({height}*{width}) doesn't match model"
                f" ({self.image_size[0]}*{self.image_size[1]})."
            )
        embeddings = self.proj(pixel_values)
        embeddings = ops.transpose(embeddings.flatten(2), 1, 2)
        return embeddings


class UdopPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models. Based on `T5PreTrainedModel`.
    """

    config_class = UdopConfig
    base_model_prefix = "transformer"
    supports_gradient_checkpointing = True
    _keep_in_fp32_modules = ["wo"]

    def _init_weights(self, module):
        """Initialize the weights"""
        factor = self.config.initializer_factor  # Used for testing weights initialization
        if isinstance(module, UdopLayerNorm):
            nn.init.constant_(module.weight, factor * 1.0)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.0, std=factor)
            if module.padding_idx is not None:
                module.weight[module.padding_idx] = 0
        elif isinstance(module, nn.Conv2d):
            # Upcast the input in `fp32` and cast it back to desired `dtype` to avoid
            # `trunc_normal_cpu` not implemented in `half` issues
            nn.init.trunc_normal_(module.weight, mean=0.0, std=factor)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, RelativePositionBiasBase):
            factor = self.config.initializer_factor
            d_model = self.config.d_model
            nn.init.normal_(module.relative_attention_bias.weight, mean=0.0, std=factor * ((d_model) ** -0.5))
        elif isinstance(module, UdopModel):
            # Mesh TensorFlow embeddings initialization
            # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/layers.py#L1624
            nn.init.normal_(module.shared.weight, mean=0.0, std=factor * 1.0)
        elif isinstance(module, UdopForConditionalGeneration):
            if hasattr(module, "lm_head") and not self.config.tie_word_embeddings:
                nn.init.normal_(module.lm_head.weight, mean=0.0, std=factor * 1.0)
        elif isinstance(module, UdopDenseActDense):
            # Mesh TensorFlow FF initialization
            # See https://github.com/tensorflow/mesh/blob/master/mesh_tensorflow/transformer/transformer_layers.py#L56
            # and https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/layers.py#L89
            nn.init.normal_(module.wi.weight, mean=0.0, std=factor * ((self.config.d_model) ** -0.5))
            if hasattr(module.wi, "bias") and module.wi.bias is not None:
                nn.init.zeros_(module.wi.bias)
            nn.init.normal_(module.wo.weight, mean=0.0, std=factor * ((self.config.d_ff) ** -0.5))
            if hasattr(module.wo, "bias") and module.wo.bias is not None:
                nn.init.zeros_(module.wo.bias)
        elif isinstance(module, UdopDenseGatedActDense):
            nn.init.normal_(module.wi_0.weight, mean=0.0, std=factor * ((self.config.d_model) ** -0.5))
            if hasattr(module.wi_0, "bias") and module.wi_0.bias is not None:
                nn.init.zeros_(module.wi_0.bias)
            nn.init.normal_(module.wi_1.weight, mean=0.0, std=factor * ((self.config.d_model) ** -0.5))
            if hasattr(module.wi_1, "bias") and module.wi_1.bias is not None:
                nn.init.zeros_(module.wi_1.bias)
            nn.init.normal_(module.wo.weight, mean=0.0, std=factor * ((self.config.d_ff) ** -0.5))
            if hasattr(module.wo, "bias") and module.wo.bias is not None:
                nn.init.zeros_(module.wo.bias)
        elif isinstance(module, UdopAttention):
            # Mesh TensorFlow attention initialization to avoid scaling before softmax
            # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/transformer/attention.py#L136
            d_model = self.config.d_model
            key_value_proj_dim = self.config.d_kv
            n_heads = self.config.num_heads
            nn.init.normal_(module.q.weight, mean=0.0, std=factor * ((d_model * key_value_proj_dim) ** -0.5))
            nn.init.normal_(module.k.weight, mean=0.0, std=factor * (d_model**-0.5))
            nn.init.normal_(module.v.weight, mean=0.0, std=factor * (d_model**-0.5))
            nn.init.normal_(module.o.weight, mean=0.0, std=factor * ((n_heads * key_value_proj_dim) ** -0.5))
            if module.has_relative_attention_bias:
                nn.init.normal_(module.relative_attention_bias.weight, mean=0.0, std=factor * ((d_model) ** -0.5))

    # Copied from transformers.models.prophetnet.modeling_prophetnet.ProphetNetPreTrainedModel._shift_right with ProphetNet->Udop
    def _shift_right(self, input_ids):
        decoder_start_token_id = self.config.decoder_start_token_id
        pad_token_id = self.config.pad_token_id

        assert decoder_start_token_id is not None, (
            "self.model.config.decoder_start_token_id has to be defined. In Udop it is usually set to the"
            " pad_token_id. See Udop docs for more information"
        )

        # shift inputs to the right
        shifted_input_ids = input_ids.new_zeros(input_ids.shape)
        shifted_input_ids[..., 1:] = input_ids[..., :-1].clone()
        shifted_input_ids[..., 0] = decoder_start_token_id

        assert pad_token_id is not None, "self.model.config.pad_token_id has to be defined."
        # replace possible -100 values in labels by `pad_token_id`
        shifted_input_ids.masked_fill_(shifted_input_ids == -100, pad_token_id)

        assert ops.all(shifted_input_ids >= 0).item(), "Verify that `shifted_input_ids` has only positive values"

        return shifted_input_ids


# Copied from transformers.models.t5.modeling_t5.T5LayerNorm with T5->Udop
class UdopLayerNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6):
        """
        Construct a layernorm module in the Udop style. No bias and no subtraction of mean.
        """
        super().__init__()
        self.weight = nn.Parameter(ops.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        # Udop uses a layer_norm which only scales and doesn't shift, which is also known as Root Mean
        # Square Layer Normalization https://arxiv.org/abs/1910.07467 thus varience is calculated
        # w/o mean and there is no bias. Additionally we want to make sure that the accumulation for
        # half-precision inputs is done in fp32

        variance = ops.mean(hidden_states.to(mindspore.float32).pow(2), -1, keepdim=True)
        hidden_states = hidden_states * ops.rsqrt(variance + self.variance_epsilon)

        # convert into half-precision if necessary
        if self.weight.dtype in [mindspore.float16, mindspore.bfloat16]:
            hidden_states = hidden_states.to(self.weight.dtype)

        return self.weight * hidden_states


# Copied from transformers.models.t5.modeling_t5.T5DenseActDense with T5->Udop
class UdopDenseActDense(nn.Module):
    def __init__(self, config: UdopConfig):
        super().__init__()
        self.wi = nn.Linear(config.d_model, config.d_ff, bias=False)
        self.wo = nn.Linear(config.d_ff, config.d_model, bias=False)
        self.dropout = nn.Dropout(config.dropout_rate)
        self.act = ACT2FN[config.dense_act_fn]

    def forward(self, hidden_states):
        hidden_states = self.wi(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.dropout(hidden_states)
        if (
            isinstance(self.wo.weight, mindspore.Tensor)
            and hidden_states.dtype != self.wo.weight.dtype
            and self.wo.weight.dtype != mindspore.int8
        ):
            hidden_states = hidden_states.to(self.wo.weight.dtype)
        hidden_states = self.wo(hidden_states)
        return hidden_states


# Copied from transformers.models.t5.modeling_t5.T5DenseGatedActDense with T5->Udop
class UdopDenseGatedActDense(nn.Module):
    def __init__(self, config: UdopConfig):
        super().__init__()
        self.wi_0 = nn.Linear(config.d_model, config.d_ff, bias=False)
        self.wi_1 = nn.Linear(config.d_model, config.d_ff, bias=False)
        self.wo = nn.Linear(config.d_ff, config.d_model, bias=False)
        self.dropout = nn.Dropout(config.dropout_rate)
        self.act = ACT2FN[config.dense_act_fn]

    def forward(self, hidden_states):
        hidden_gelu = self.act(self.wi_0(hidden_states))
        hidden_linear = self.wi_1(hidden_states)
        hidden_states = hidden_gelu * hidden_linear
        hidden_states = self.dropout(hidden_states)

        # To make 8bit quantization work for google/flan-t5-xxl, self.wo is kept in float32.
        # See https://github.com/huggingface/transformers/issues/20287
        # we also make sure the weights are not in `int8` in case users will force `_keep_in_fp32_modules` to be `None``
        if (
            isinstance(self.wo.weight, mindspore.Tensor)
            and hidden_states.dtype != self.wo.weight.dtype
            and self.wo.weight.dtype != mindspore.int8
        ):
            hidden_states = hidden_states.to(self.wo.weight.dtype)

        hidden_states = self.wo(hidden_states)
        return hidden_states


# Copied from transformers.models.t5.modeling_t5.T5LayerFF with T5->Udop
class UdopLayerFF(nn.Module):
    def __init__(self, config: UdopConfig):
        super().__init__()
        if config.is_gated_act:
            self.DenseReluDense = UdopDenseGatedActDense(config)
        else:
            self.DenseReluDense = UdopDenseActDense(config)

        self.layer_norm = UdopLayerNorm(config.d_model, eps=config.layer_norm_epsilon)
        self.dropout = nn.Dropout(config.dropout_rate)

    def forward(self, hidden_states):
        forwarded_states = self.layer_norm(hidden_states)
        forwarded_states = self.DenseReluDense(forwarded_states)
        hidden_states = hidden_states + self.dropout(forwarded_states)
        return hidden_states


# Copied from transformers.models.t5.modeling_t5.T5Attention with T5->Udop
class UdopAttention(nn.Module):
    def __init__(self, config: UdopConfig, has_relative_attention_bias=False):
        super().__init__()
        self.is_decoder = config.is_decoder
        self.has_relative_attention_bias = has_relative_attention_bias
        self.relative_attention_num_buckets = config.relative_attention_num_buckets
        self.relative_attention_max_distance = config.relative_attention_max_distance
        self.d_model = config.d_model
        self.key_value_proj_dim = config.d_kv
        self.n_heads = config.num_heads
        self.dropout = config.dropout_rate
        self.inner_dim = self.n_heads * self.key_value_proj_dim

        # Mesh TensorFlow initialization to avoid scaling before softmax
        self.q = nn.Linear(self.d_model, self.inner_dim, bias=False)
        self.k = nn.Linear(self.d_model, self.inner_dim, bias=False)
        self.v = nn.Linear(self.d_model, self.inner_dim, bias=False)
        self.o = nn.Linear(self.inner_dim, self.d_model, bias=False)

        if self.has_relative_attention_bias:
            self.relative_attention_bias = nn.Embedding(self.relative_attention_num_buckets, self.n_heads)
        self.pruned_heads = set()
        self.gradient_checkpointing = False

    def prune_heads(self, heads):
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(
            heads, self.n_heads, self.key_value_proj_dim, self.pruned_heads
        )
        # Prune linear layers
        self.q = prune_linear_layer(self.q, index)
        self.k = prune_linear_layer(self.k, index)
        self.v = prune_linear_layer(self.v, index)
        self.o = prune_linear_layer(self.o, index, dim=1)
        # Update hyper params
        self.n_heads = self.n_heads - len(heads)
        self.inner_dim = self.key_value_proj_dim * self.n_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    @staticmethod
    def _relative_position_bucket(relative_position, bidirectional=True, num_buckets=32, max_distance=128):
        """
        Adapted from Mesh Tensorflow:
        https://github.com/tensorflow/mesh/blob/0cb87fe07da627bf0b7e60475d59f95ed6b5be3d/mesh_tensorflow/transformer/transformer_layers.py#L593

        Translate relative position to a bucket number for relative attention. The relative position is defined as
        memory_position - query_position, i.e. the distance in tokens from the attending position to the attended-to
        position. If bidirectional=False, then positive relative positions are invalid. We use smaller buckets for
        small absolute relative_position and larger buckets for larger absolute relative_positions. All relative
        positions >=max_distance map to the same bucket. All relative positions <=-max_distance map to the same bucket.
        This should allow for more graceful generalization to longer sequences than the model has been trained on

        Args:
            relative_position: an int32 Tensor
            bidirectional: a boolean - whether the attention is bidirectional
            num_buckets: an integer
            max_distance: an integer

        Returns:
            a Tensor with the same shape as relative_position, containing int32 values in the range [0, num_buckets)
        """
        relative_buckets = 0
        if bidirectional:
            num_buckets //= 2
            relative_buckets += (relative_position > 0).to(mindspore.int64) * num_buckets
            relative_position = ops.abs(relative_position)
        else:
            relative_position = -ops.minimum(relative_position, ops.zeros_like(relative_position))
        # now relative_position is in the range [0, inf)

        # half of the buckets are for exact increments in positions
        max_exact = num_buckets // 2
        is_small = relative_position < max_exact

        # The other half of the buckets are for logarithmically bigger bins in positions up to max_distance
        relative_position_if_large = max_exact + (
            ops.log(relative_position.float() / max_exact)
            / math.log(max_distance / max_exact)
            * (num_buckets - max_exact)
        ).to(mindspore.int64)
        relative_position_if_large = ops.minimum(
            relative_position_if_large, ops.full_like(relative_position_if_large, num_buckets - 1)
        )

        relative_buckets += ops.where(is_small, relative_position, relative_position_if_large)
        return relative_buckets

    def compute_bias(self, query_length, key_length):
        """Compute binned relative position bias"""
        context_position = ops.arange(query_length, dtype=mindspore.int64)[:, None]
        memory_position = ops.arange(key_length, dtype=mindspore.int64)[None, :]
        relative_position = memory_position - context_position  # shape (query_length, key_length)
        relative_position_bucket = self._relative_position_bucket(
            relative_position,  # shape (query_length, key_length)
            bidirectional=(not self.is_decoder),
            num_buckets=self.relative_attention_num_buckets,
            max_distance=self.relative_attention_max_distance,
        )
        values = self.relative_attention_bias(relative_position_bucket)  # shape (query_length, key_length, num_heads)
        values = values.permute([2, 0, 1]).unsqueeze(0)  # shape (1, num_heads, query_length, key_length)
        return values

    def forward(
        self,
        hidden_states,
        mask=None,
        key_value_states=None,
        position_bias=None,
        past_key_value=None,
        layer_head_mask=None,
        query_length=None,
        use_cache=False,
        output_attentions=False,
    ):
        """
        Self-attention (if key_value_states is None) or attention over source sentence (provided by key_value_states).
        """
        # Input is (batch_size, seq_length, dim)
        # Mask is (batch_size, key_length) (non-causal) or (batch_size, key_length, key_length)
        # past_key_value[0] is (batch_size, n_heads, q_len - 1, dim_per_head)
        batch_size, seq_length = hidden_states.shape[:2]

        real_seq_length = seq_length

        if past_key_value is not None:
            if len(past_key_value) != 2:
                raise ValueError(
                    f"past_key_value should have 2 past states: keys and values. Got { len(past_key_value)} past states"
                )
            real_seq_length += past_key_value[0].shape[2] if query_length is None else query_length

        key_length = real_seq_length if key_value_states is None else key_value_states.shape[1]

        def shape(states):
            """projection"""
            return ops.transpose(states.view(batch_size, -1, self.n_heads, self.key_value_proj_dim), 1, 2)

        def unshape(states):
            """reshape"""
            return ops.transpose(states, 1, 2).view(batch_size, -1, self.inner_dim)

        def project(hidden_states, proj_layer, key_value_states, past_key_value):
            """projects hidden states correctly to key/query states"""
            if key_value_states is None:
                # self-attn
                # (batch_size, n_heads, seq_length, dim_per_head)
                hidden_states = shape(proj_layer(hidden_states))
            elif past_key_value is None:
                # cross-attn
                # (batch_size, n_heads, seq_length, dim_per_head)
                hidden_states = shape(proj_layer(key_value_states))

            if past_key_value is not None:
                if key_value_states is None:
                    # self-attn
                    # (batch_size, n_heads, key_length, dim_per_head)
                    hidden_states = ops.cat([past_key_value, hidden_states], dim=2)
                elif past_key_value.shape[2] != key_value_states.shape[1]:
                    # checking that the `sequence_length` of the `past_key_value` is the same as
                    # the provided `key_value_states` to support prefix tuning
                    # cross-attn
                    # (batch_size, n_heads, seq_length, dim_per_head)
                    hidden_states = shape(proj_layer(key_value_states))
                else:
                    # cross-attn
                    hidden_states = past_key_value
            return hidden_states

        # get query states
        query_states = shape(self.q(hidden_states))  # (batch_size, n_heads, seq_length, dim_per_head)

        # get key/value states
        key_states = project(
            hidden_states, self.k, key_value_states, past_key_value[0] if past_key_value is not None else None
        )
        value_states = project(
            hidden_states, self.v, key_value_states, past_key_value[1] if past_key_value is not None else None
        )

        # compute scores
        scores = ops.matmul(
            query_states, ops.transpose(key_states, 3, 2)
        )  # equivalent of ops.einsum("bnqd,bnkd->bnqk", query_states, key_states), compatible with onnx op>9

        if position_bias is None:
            if not self.has_relative_attention_bias:
                position_bias = ops.zeros(
                    (1, self.n_heads, real_seq_length, key_length), dtype=scores.dtype
                )
                if self.gradient_checkpointing and self.training:
                    position_bias.requires_grad = True
            else:
                position_bias = self.compute_bias(real_seq_length, key_length)

            # if key and values are already calculated
            # we want only the last query position bias
            if past_key_value is not None:
                position_bias = position_bias[:, :, -hidden_states.shape[1] :, :]

            if mask is not None:
                position_bias = position_bias + mask  # (batch_size, n_heads, seq_length, key_length)

        if self.pruned_heads:
            mask = ops.ones(position_bias.shape[1])
            mask[list(self.pruned_heads)] = 0
            position_bias_masked = position_bias[:, mask.bool()]
        else:
            position_bias_masked = position_bias

        scores += position_bias_masked
        attn_weights = nn.functional.softmax(scores.float(), dim=-1).type_as(
            scores
        )  # (batch_size, n_heads, seq_length, key_length)
        attn_weights = nn.functional.dropout(
            attn_weights, p=self.dropout, training=self.training
        )  # (batch_size, n_heads, seq_length, key_length)

        # Mask heads if we want to
        if layer_head_mask is not None:
            attn_weights = attn_weights * layer_head_mask

        attn_output = unshape(ops.matmul(attn_weights, value_states))  # (batch_size, seq_length, dim)
        attn_output = self.o(attn_output)

        present_key_value_state = (key_states, value_states) if (self.is_decoder and use_cache) else None
        outputs = (attn_output,) + (present_key_value_state,) + (position_bias,)

        if output_attentions:
            outputs = outputs + (attn_weights,)
        return outputs


# Copied from transformers.models.t5.modeling_t5.T5LayerSelfAttention with T5->Udop
class UdopLayerSelfAttention(nn.Module):
    def __init__(self, config, has_relative_attention_bias=False):
        super().__init__()
        self.SelfAttention = UdopAttention(config, has_relative_attention_bias=has_relative_attention_bias)
        self.layer_norm = UdopLayerNorm(config.d_model, eps=config.layer_norm_epsilon)
        self.dropout = nn.Dropout(config.dropout_rate)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        position_bias=None,
        layer_head_mask=None,
        past_key_value=None,
        use_cache=False,
        output_attentions=False,
    ):
        normed_hidden_states = self.layer_norm(hidden_states)
        attention_output = self.SelfAttention(
            normed_hidden_states,
            mask=attention_mask,
            position_bias=position_bias,
            layer_head_mask=layer_head_mask,
            past_key_value=past_key_value,
            use_cache=use_cache,
            output_attentions=output_attentions,
        )
        hidden_states = hidden_states + self.dropout(attention_output[0])
        outputs = (hidden_states,) + attention_output[1:]  # add attentions if we output them
        return outputs


# Copied from transformers.models.t5.modeling_t5.T5LayerCrossAttention with T5->Udop
class UdopLayerCrossAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.EncDecAttention = UdopAttention(config, has_relative_attention_bias=False)
        self.layer_norm = UdopLayerNorm(config.d_model, eps=config.layer_norm_epsilon)
        self.dropout = nn.Dropout(config.dropout_rate)

    def forward(
        self,
        hidden_states,
        key_value_states,
        attention_mask=None,
        position_bias=None,
        layer_head_mask=None,
        past_key_value=None,
        use_cache=False,
        query_length=None,
        output_attentions=False,
    ):
        normed_hidden_states = self.layer_norm(hidden_states)
        attention_output = self.EncDecAttention(
            normed_hidden_states,
            mask=attention_mask,
            key_value_states=key_value_states,
            position_bias=position_bias,
            layer_head_mask=layer_head_mask,
            past_key_value=past_key_value,
            use_cache=use_cache,
            query_length=query_length,
            output_attentions=output_attentions,
        )
        layer_output = hidden_states + self.dropout(attention_output[0])
        outputs = (layer_output,) + attention_output[1:]  # add attentions if we output them
        return outputs


# Copied from transformers.models.t5.modeling_t5.T5Block with T5->Udop
class UdopBlock(nn.Module):
    def __init__(self, config, has_relative_attention_bias=False):
        super().__init__()
        self.is_decoder = config.is_decoder
        self.layer = nn.ModuleList()
        self.layer.append(UdopLayerSelfAttention(config, has_relative_attention_bias=has_relative_attention_bias))
        if self.is_decoder:
            self.layer.append(UdopLayerCrossAttention(config))

        self.layer.append(UdopLayerFF(config))

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        position_bias=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        encoder_decoder_position_bias=None,
        layer_head_mask=None,
        cross_attn_layer_head_mask=None,
        past_key_value=None,
        use_cache=False,
        output_attentions=False,
        return_dict=True,
    ):
        if past_key_value is not None:
            if not self.is_decoder:
                logger.warning("`past_key_values` is passed to the encoder. Please make sure this is intended.")
            expected_num_past_key_values = 2 if encoder_hidden_states is None else 4

            if len(past_key_value) != expected_num_past_key_values:
                raise ValueError(
                    f"There should be {expected_num_past_key_values} past states. "
                    f"{'2 (key / value) for cross attention. ' if expected_num_past_key_values == 4 else ''}"
                    f"Got {len(past_key_value)} past key / value states"
                )

            self_attn_past_key_value = past_key_value[:2]
            cross_attn_past_key_value = past_key_value[2:]
        else:
            self_attn_past_key_value, cross_attn_past_key_value = None, None

        self_attention_outputs = self.layer[0](
            hidden_states,
            attention_mask=attention_mask,
            position_bias=position_bias,
            layer_head_mask=layer_head_mask,
            past_key_value=self_attn_past_key_value,
            use_cache=use_cache,
            output_attentions=output_attentions,
        )
        hidden_states, present_key_value_state = self_attention_outputs[:2]
        attention_outputs = self_attention_outputs[2:]  # Keep self-attention outputs and relative position weights

        # clamp inf values to enable fp16 training
        if hidden_states.dtype == mindspore.float16:
            clamp_value = ops.where(
                ops.isinf(hidden_states).any(),
                float(ops.finfo(hidden_states.dtype).max) - 1000,
                float(ops.finfo(hidden_states.dtype).max),
            )
            hidden_states = ops.clamp(hidden_states, min=-clamp_value, max=clamp_value)

        do_cross_attention = self.is_decoder and encoder_hidden_states is not None
        if do_cross_attention:
            # the actual query length is unknown for cross attention
            # if using past key value states. Need to inject it here
            if present_key_value_state is not None:
                query_length = present_key_value_state[0].shape[2]
            else:
                query_length = None

            cross_attention_outputs = self.layer[1](
                hidden_states,
                key_value_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                position_bias=encoder_decoder_position_bias,
                layer_head_mask=cross_attn_layer_head_mask,
                past_key_value=cross_attn_past_key_value,
                query_length=query_length,
                use_cache=use_cache,
                output_attentions=output_attentions,
            )
            hidden_states = cross_attention_outputs[0]

            # clamp inf values to enable fp16 training
            if hidden_states.dtype == mindspore.float16:
                clamp_value = ops.where(
                    ops.isinf(hidden_states).any(),
                    float(ops.finfo(hidden_states.dtype).max) - 1000,
                    float(ops.finfo(hidden_states.dtype).max),
                )
                hidden_states = ops.clamp(hidden_states, min=-clamp_value, max=clamp_value)

            # Combine self attn and cross attn key value states
            if present_key_value_state is not None:
                present_key_value_state = present_key_value_state + cross_attention_outputs[1]

            # Keep cross-attention outputs and relative position weights
            attention_outputs = attention_outputs + cross_attention_outputs[2:]

        # Apply Feed Forward layer
        hidden_states = self.layer[-1](hidden_states)

        # clamp inf values to enable fp16 training
        if hidden_states.dtype == mindspore.float16:
            clamp_value = ops.where(
                ops.isinf(hidden_states).any(),
                float(ops.finfo(hidden_states.dtype).max) - 1000,
                float(ops.finfo(hidden_states.dtype).max),
            )
            hidden_states = ops.clamp(hidden_states, min=-clamp_value, max=clamp_value)

        outputs = (hidden_states,)

        if use_cache:
            outputs = outputs + (present_key_value_state,) + attention_outputs
        else:
            outputs = outputs + attention_outputs

        return outputs  # hidden-states, present_key_value_states, (self-attention position bias), (self-attention weights), (cross-attention position bias), (cross-attention weights)


class UdopCellEmbeddings(nn.Module):
    def __init__(self, max_2d_position_embeddings=501, hidden_size=1024):
        super(UdopCellEmbeddings, self).__init__()
        self.max_2d_position_embeddings = max_2d_position_embeddings

        self.x_position_embeddings = nn.Embedding(max_2d_position_embeddings, hidden_size)
        self.y_position_embeddings = nn.Embedding(max_2d_position_embeddings, hidden_size)

    def forward(self, bbox):
        bbox = ops.clip(bbox, 0.0, 1.0)
        bbox = (bbox * (self.max_2d_position_embeddings - 1)).long()
        left_position_embeddings = self.x_position_embeddings(bbox[:, :, 0])
        upper_position_embeddings = self.y_position_embeddings(bbox[:, :, 1])
        right_position_embeddings = self.x_position_embeddings(bbox[:, :, 2])
        lower_position_embeddings = self.y_position_embeddings(bbox[:, :, 3])

        embeddings = (
            left_position_embeddings
            + upper_position_embeddings
            + right_position_embeddings
            + lower_position_embeddings
        )

        return embeddings


# get function for bucket computation
# protected member access seems to be lesser evil than copy paste whole function
get_relative_position_bucket = UdopAttention._relative_position_bucket
AUGMENTATION_RANGE = (0.80, 1.25)


class RelativePositionBiasBase(nn.Module, ABC):
    """
    Base class of relative biases.

    Args:
        num_heads (`int`):
            Number of attention heads in the model, it will create embeddings of size `num_heads`, which will be added to the scores of each token pair.
        relative_attention_num_buckets (`int`, *optional*, defaults to 32):
            Pair token metric (distance in the sequence, distance in pixels etc.) will be bucketed, parameter is defining number of such
            buckets.
        bidirectional (`bool`, *optional*, defaults to `True`):
            Whether the distance should be bidirectional for a pair of tokens. If `False`, then distance(tok1, tok2) == distance(tok2, tok1).
        scaling_factor (`int`, *optional*, defaults to 1):
            Defining factor which will be used to scale relative distance.
        max_distance (`int`, *optional*, defaults to 128):
            All distances above this value will end up in the one/same bucket.
        augmentation (`bool`, *optional*, defaults to `False`):
            Whether to multiply relative distances by a random scalar.
        expand (`bool`, *optional*, defaults to `False`):
            Whether to expand an existing pretrained model with subsequent additions of prefix_bucket.
    """

    def __init__(
        self,
        num_heads=None,
        relative_attention_num_buckets=32,
        bidirectional=True,
        scaling_factor=1,
        max_distance=128,
        level="tokens",
        augmentation=False,
        prefix_bucket=False,
        expand=False,
    ):
        super(RelativePositionBiasBase, self).__init__()
        self.prefix_bucket = prefix_bucket
        self.augmentation = augmentation
        self.level = level
        self.max_distance = max_distance
        self.scaling_factor = scaling_factor
        self.bidirectional = bidirectional
        self.num_heads = num_heads
        self.expand = expand
        self.relative_attention_num_buckets = relative_attention_num_buckets
        extra_head = 2 if prefix_bucket and not self.expand else 0
        self.relative_attention_bias = nn.Embedding(self.relative_attention_num_buckets + extra_head, self.num_heads)

    @abstractmethod
    def prepare_input(
        self,
        attention_mask: Optional[Tensor] = None,
        bbox: Optional[Dict[str, Any]] = None,
    ) -> Tensor:
        pass

    def get_bucket(self, attention_mask: Optional[Tensor] = None, bbox: Optional[Dict[str, Any]] = None) -> Tensor:
        relative_position = self.prepare_input(attention_mask, bbox)
        rp_bucket: Tensor = get_relative_position_bucket(
            relative_position,
            bidirectional=self.bidirectional,
            num_buckets=self.relative_attention_num_buckets,
            max_distance=self.max_distance,
        )
        return rp_bucket

    def get_relative_position(self, positions):
        context_position = positions[:, :, None]
        memory_position = positions[:, None, :]
        relative_position = memory_position - context_position
        if self.augmentation and self.training:
            relative_position *= random.uniform(*AUGMENTATION_RANGE)
        relative_position *= self.scaling_factor

        return relative_position.to(mindspore.int64)

    def forward(self, attention_mask: Optional[Tensor] = None, bbox: Optional[Dict[str, Any]] = None) -> Tensor:
        # re-using pretrained model with subsequent addition of prefix_bucket
        if self.expand and self.prefix_bucket:
            new_bias = nn.Embedding(self.relative_attention_num_buckets + 2, self.num_heads)
            new_bias.weight[: self.relative_attention_num_buckets] = self.relative_attention_bias.weight
            new_bias.weight[self.relative_attention_num_buckets :] = 0.1
            self.relative_attention_bias = new_bias
            self.expand = False

        rp_bucket = self.get_bucket(attention_mask, bbox)

        if self.prefix_bucket:
            if rp_bucket.shape[0] == 1 and attention_mask.shape[0] > 1:
                rp_bucket = rp_bucket.tile((attention_mask.shape[0], 1, 1))
            # based on assumption that prefix bboxes are negative
            is_prefix = bbox[:, :, 1] < 0
            num_prefix = is_prefix.sum(-1)
            for idx, num_prefix_row in enumerate(num_prefix.asnumpy()):
                rp_bucket[idx, :num_prefix_row, num_prefix_row:] = self.relative_attention_num_buckets
                rp_bucket[idx, num_prefix_row:, :num_prefix_row] = self.relative_attention_num_buckets + 1

        values: Tensor = self.relative_attention_bias(rp_bucket)
        if values.dim() != 4:
            raise ValueError("Wrong dimension of values tensor")
        values = values.permute([0, 3, 1, 2])

        return values


class RelativePositionBias1D(RelativePositionBiasBase):
    def __init__(self, scaling_factor=1, max_distance=128, **kwargs):
        """
        Reimplementation of T5 relative position bias. Distance between given tokens is their distance in the sequence.
        Parameters are the same as in base class
        """
        super().__init__(scaling_factor=scaling_factor, max_distance=max_distance, **kwargs)

    def prepare_input(self, attention_mask: Optional[Tensor] = None, bbox: Optional[Dict[str, Any]] = None) -> Tensor:
        if self.scaling_factor != 1:
            raise ValueError("No need to scale 1d features")
        relative_position = self.get_relative_position(
            ops.arange(attention_mask.shape[1], dtype=mindspore.int64)[None, :]
        )

        return relative_position


class RelativePositionBiasHorizontal(RelativePositionBiasBase):
    def __init__(self, scaling_factor=100, max_distance=100, **kwargs):
        """
        Represents in the bucket embeddings horizontal distance between two tokens. Parameters are the same as in base
        class
        """
        super().__init__(scaling_factor=scaling_factor, max_distance=max_distance, **kwargs)

    def prepare_input(self, attention_mask: Optional[Tensor] = None, bbox: Optional[Dict[str, Any]] = None) -> Tensor:
        if self.scaling_factor <= 1.0:
            raise ValueError("Need to scale the values of bboxes, as there are in small (0,1) range")
        if bbox is None:
            raise ValueError("Bbox is required for horizontal relative position bias")
        # get x positions of left point of bbox
        horizontal_position: Tensor = ops.mean(bbox[:, :, [0, 2]], dim=-1)

        return self.get_relative_position(horizontal_position)


class RelativePositionBiasVertical(RelativePositionBiasBase):
    def __init__(self, scaling_factor=100, max_distance=100, **kwargs):
        """
        Represents in the bucket embeddings vertical distance between two tokens. Parameters are the same as in base
        class
        """
        super().__init__(scaling_factor=scaling_factor, max_distance=max_distance, **kwargs)

    def prepare_input(self, attention_mask: Optional[Tensor] = None, bbox: Optional[Dict[str, Any]] = None) -> Tensor:
        if self.scaling_factor <= 1.0:
            raise ValueError("Need to scale the values of bboxes, as there are in small (0,1) range")
        if bbox is None:
            raise ValueError("Bbox is required for vertical relative position bias")
        # get y positions of middle of bbox
        vertical_position: Tensor = ops.mean(bbox[:, :, [1, 3]], dim=-1)

        return self.get_relative_position(vertical_position)


class RelativePositionBiasAggregated(nn.Module):
    def __init__(self, modules: Sequence[RelativePositionBiasBase]):
        """
        Class which sums up various computed biases.

        Args:
            modules (Sequence[RelativePositionBiasBase]):
                List of relative bias modules.
        """
        super().__init__()
        self.biases = nn.ModuleList(modules)

    def forward(
        self, attention_mask: Optional[Tensor] = None, bbox: Optional[Dict[str, Any]] = None
    ) -> Union[float, Tensor]:
        output = 0.0
        for bias in self.biases:  # type: ignore
            output = bias(attention_mask, bbox) + output

        return output


BIAS_CLASSES = {
    "1d": RelativePositionBias1D,
    "horizontal": RelativePositionBiasHorizontal,
    "vertical": RelativePositionBiasVertical,
}


def create_relative_bias(config: UdopConfig) -> Sequence[RelativePositionBiasBase]:
    """
    Creates empty list or one/multiple relative biases.

    :param config: Model's configuration :return: Sequence with created bias modules.
    """
    bias_list = []
    if hasattr(config, "relative_bias_args"):
        for bias_kwargs_org in config.relative_bias_args:
            bias_kwargs = deepcopy(bias_kwargs_org)
            bias_type = bias_kwargs.pop("type")
            model_num_heads = config.num_heads if hasattr(config, "num_heads") else config.num_attention_heads
            if "num_heads" in bias_kwargs:
                if bias_kwargs["num_heads"] != model_num_heads:
                    raise ValueError("Number of heads must match num of heads in the model")
            else:
                bias_kwargs["num_heads"] = model_num_heads
            bias_list.append(BIAS_CLASSES[bias_type](**bias_kwargs))  # type: ignore

    return bias_list


class UdopStack(UdopPreTrainedModel):
    """
    This class is based on `T5Stack`, but modified to take into account the image modality as well as 2D position
    embeddings.
    """

    def __init__(self, config, embed_tokens=None, embed_patches=None):
        super().__init__(config)

        self.embed_tokens = embed_tokens
        self.embed_patches = embed_patches
        self.is_decoder = config.is_decoder
        self._max_length = config.max_length
        self.num_layers = config.num_layers

        self.block = nn.ModuleList(
            [UdopBlock(config, has_relative_attention_bias=bool(i == 0)) for i in range(self.num_layers)]
        )
        self.final_layer_norm = UdopLayerNorm(config.d_model, eps=config.layer_norm_epsilon)

        self.dropout = nn.Dropout(config.dropout_rate)

        if not self.is_decoder:
            self.cell_2d_embedding = UdopCellEmbeddings(config.max_2d_position_embeddings, config.hidden_size)

        # get weights from encoder position bias
        self.relative_bias = self._get_relative_bias(config)

    def _tie_weights(self):
        for bias in self.relative_bias.biases:
            if isinstance(bias, RelativePositionBias1D):
                self._tie_or_clone_weights(
                    bias.relative_attention_bias, self.block[0].layer[0].SelfAttention.relative_attention_bias
                )

    @staticmethod
    def _get_relative_bias(config: UdopConfig) -> RelativePositionBiasAggregated:
        relative_bias_list = create_relative_bias(config)
        return RelativePositionBiasAggregated(relative_bias_list)

    def get_input_embeddings(self):
        return self.embed_tokens

    def get_output_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, new_embeddings):
        self.embed_tokens = new_embeddings

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        bbox=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        inputs_embeds=None,
        pixel_values=None,
        visual_bbox=None,
        image_embeddings=None,
        position_bias=None,
        head_mask=None,
        cross_attn_head_mask=None,
        past_key_values=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # input embeddings processing

        if input_ids is not None and inputs_embeds is not None:
            err_msg_prefix = "decoder_" if self.is_decoder else ""
            raise ValueError(
                f"You cannot specify both {err_msg_prefix}inputs and {err_msg_prefix}inputs_embeds at the same time"
            )
        elif input_ids is not None and ops.numel(input_ids) > 0:
            input_shape = input_ids.shape
            input_ids = input_ids.view(-1, input_shape[-1])
        elif inputs_embeds is None and input_ids is not None and ops.numel(input_ids) == 0:
            input_ids = ops.full((4, 1024), self.config.pad_token_id, dtype=input_ids.dtype)
            attention_mask = ops.zeros((4, 1024), dtype=input_ids.dtype)
            bbox = ops.zeros((4, 1024, 4), dtype=input_ids.dtype)
            input_shape = input_ids.shape
            position_bias = ops.zeros_like(self.get_extended_attention_mask(attention_mask, input_shape))
            # encoder_attention_mask = attention_mask
            logger.warning("Empty batch")
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
        else:
            err_msg_prefix = "decoder_" if self.is_decoder else ""
            raise ValueError(f"You have to specify either {err_msg_prefix}inputs or {err_msg_prefix}inputs_embeds")

        if inputs_embeds is None:
            if self.embed_tokens is None:
                raise ValueError("You have to intialize the model with valid token embeddings")
            inputs_embeds = self.embed_tokens(input_ids)

        if pixel_values is not None:
            image_embeddings = self.embed_patches(pixel_values)

        if image_embeddings is not None:
            # combine visual and OCR text embeddings
            num_patches = self.config.image_size // self.config.patch_size
            inputs_embeds, bbox, attention_mask = combine_image_text_embeddings(
                image_embeddings,
                inputs_embeds,
                bbox,
                visual_bbox,
                attention_mask,
                num_patches,
                0,
                self.config.image_size,
                self.config.patch_size,
            )
            input_shape = inputs_embeds.shape[:-1]

        if not self.is_decoder and bbox is not None:
            inputs_embeds += self.cell_2d_embedding(bbox)

        batch_size, seq_length = input_shape

        # required mask seq length can be calculated via length of past
        mask_seq_length = past_key_values[0][0].shape[2] + seq_length if past_key_values is not None else seq_length

        if use_cache is True:
            assert self.is_decoder, "`use_cache` can only be set to `True` if {} is used as a decoder".format(self)

        if attention_mask is None:
            attention_mask = ops.ones(batch_size, mask_seq_length)
        if self.is_decoder and encoder_attention_mask is None and encoder_hidden_states is not None:
            encoder_seq_length = encoder_hidden_states.shape[1]
            encoder_attention_mask = ops.ones(
                batch_size, encoder_seq_length, dtype=mindspore.int64
            )

        # initialize past_key_values with `None` if past does not exist
        if past_key_values is None:
            past_key_values = [None] * len(self.block)

        # ourselves in which case we just need to make it broadcastable to all heads.
        extended_attention_mask = self.get_extended_attention_mask(attention_mask, input_shape)

        if self.is_decoder and encoder_attention_mask is not None:
            encoder_extended_attention_mask = self.invert_attention_mask(encoder_attention_mask)
        else:
            encoder_extended_attention_mask = None

        # Prepare head mask if needed
        head_mask = self.get_head_mask(head_mask, self.num_layers)
        present_key_value_states = () if use_cache else None
        all_hidden_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        all_cross_attentions = () if (output_attentions and self.is_decoder) else None

        if self.is_decoder:  # modified lines
            position_bias = None
        else:
            position_bias = self.relative_bias(attention_mask=attention_mask, bbox=bbox)
            position_bias = position_bias + extended_attention_mask
        encoder_decoder_position_bias = None

        hidden_states = inputs_embeds

        hidden_states = self.dropout(hidden_states)

        for i, (layer_module, past_key_value) in enumerate(zip(self.block, past_key_values)):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_outputs = layer_module(
                hidden_states,
                attention_mask=extended_attention_mask,
                position_bias=position_bias,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=encoder_extended_attention_mask,
                encoder_decoder_position_bias=encoder_decoder_position_bias,
                layer_head_mask=head_mask[i],
                past_key_value=past_key_value,
                use_cache=use_cache,
                output_attentions=output_attentions,
            )
            # layer_outputs is a tuple with:
            # hidden-states, key-value-states, (self-attention weights), (self-attention position bias), (cross-attention weights), (cross-attention position bias)
            if use_cache is False:  # MP fixes
                layer_outputs = layer_outputs[:1] + (None,) + layer_outputs[1:]
            hidden_states, present_key_value_state = layer_outputs[:2]

            # We share the position biases between the layers - the first layer store them
            # layer_outputs = hidden-states, key-value-states (self-attention weights),
            # (self-attention position bias), (cross-attention weights), (cross-attention position bias)

            position_bias = layer_outputs[2]
            if self.is_decoder and encoder_hidden_states is not None:
                encoder_decoder_position_bias = layer_outputs[4 if output_attentions else 3]
            # append next layer key value states
            if use_cache:
                present_key_value_states = present_key_value_states + (present_key_value_state,)

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[2],)  # We keep only self-attention weights for now
                if self.is_decoder:
                    all_cross_attentions = all_cross_attentions + (layer_outputs[5],)

        hidden_states = self.final_layer_norm(hidden_states)
        hidden_states = self.dropout(hidden_states)

        # Add last layer
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [
                    hidden_states,
                    attention_mask,
                    present_key_value_states,
                    all_hidden_states,
                    all_attentions,
                    all_cross_attentions,
                ]
                if v is not None
            )

        return BaseModelOutputWithAttentionMask(
            last_hidden_state=hidden_states,
            attention_mask=attention_mask,
            past_key_values=present_key_value_states,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
            cross_attentions=all_cross_attentions,
        )


class UdopModel(UdopPreTrainedModel):
    _tied_weights_keys = [
        "encoder.embed_tokens.weight",
        "decoder.embed_tokens.weight",
        "encoder.embed_patches.proj.weight",
        "encoder.embed_patches.proj.bias",
        "encoder.relative_bias.biases.0.relative_attention_bias.weight",
        "decoder.relative_bias.biases.0.relative_attention_bias.weight",
    ]

    def __init__(self, config):
        super(UdopModel, self).__init__(config)

        # text and image embeddings
        self.shared = nn.Embedding(config.vocab_size, config.d_model)
        self.patch_embed = UdopPatchEmbeddings(config)

        encoder_config = deepcopy(config)
        encoder_config.is_decoder = False
        encoder_config.use_cache = False
        encoder_config.is_encoder_decoder = False
        self.encoder = UdopStack(encoder_config, self.shared, self.patch_embed)

        decoder_config = deepcopy(config)
        decoder_config.is_decoder = True
        decoder_config.is_encoder_decoder = False
        decoder_config.num_layers = config.num_decoder_layers
        self.decoder = UdopStack(decoder_config, self.shared)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.shared

    def set_input_embeddings(self, new_embeddings):
        self.shared = new_embeddings
        self.encoder.set_input_embeddings(new_embeddings)
        self.decoder.set_input_embeddings(new_embeddings)

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder

    def forward(
        self,
        input_ids: Tensor = None,
        attention_mask: Tensor = None,
        bbox: Dict[str, Any] = None,
        pixel_values: Optional[Tensor] = None,
        visual_bbox: Dict[str, Any] = None,
        decoder_input_ids: Optional[Tensor] = None,
        decoder_attention_mask: Optional[Tensor] = None,
        inputs_embeds: Optional[Tensor] = None,
        encoder_outputs: Optional[Tensor] = None,
        past_key_values: Optional[Tensor] = None,
        head_mask: Optional[Tensor] = None,
        decoder_inputs_embeds: Optional[Tensor] = None,
        decoder_head_mask: Optional[Tensor] = None,
        cross_attn_head_mask: Optional[Tensor] = None,
        use_cache=True,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Tuple[Tensor, ...]:
        r"""
        Returns:

        Example:

        ```python
        >>> from transformers import AutoProcessor, AutoModel
        >>> from datasets import load_dataset
        >>> import torch

        >>> # load model and processor
        >>> # in this case, we already have performed OCR ourselves
        >>> # so we initialize the processor with `apply_ocr=False`
        >>> processor = AutoProcessor.from_pretrained("microsoft/udop-large", apply_ocr=False)
        >>> model = AutoModel.from_pretrained("microsoft/udop-large")

        >>> # load an example image, along with the words and coordinates
        >>> # which were extracted using an OCR engine
        >>> dataset = load_dataset("nielsr/funsd-layoutlmv3", split="train", trust_remote_code=True)
        >>> example = dataset[0]
        >>> image = example["image"]
        >>> words = example["tokens"]
        >>> boxes = example["bboxes"]
        >>> inputs = processor(image, words, boxes=boxes, return_tensors="ms")

        >>> decoder_input_ids = mindspore.tensor([[model.config.decoder_start_token_id]])

        >>> # forward pass
        >>> outputs = model(**inputs, decoder_input_ids=decoder_input_ids)
        >>> last_hidden_states = outputs.last_hidden_state
        >>> list(last_hidden_states.shape)
        [1, 1, 1024]
        ```"""
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # Encode if needed (training, first prediction pass)
        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                attention_mask=attention_mask,
                bbox=bbox,
                pixel_values=pixel_values,
                visual_bbox=visual_bbox,
                inputs_embeds=inputs_embeds,
                head_mask=head_mask,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )

        hidden_states = encoder_outputs[0]
        encoder_attention_mask = encoder_outputs.attention_mask if return_dict else encoder_outputs[1]

        # Decode
        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            inputs_embeds=decoder_inputs_embeds,
            past_key_values=past_key_values,
            encoder_hidden_states=hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        if not return_dict:
            # we filter out the attention mask
            decoder_outputs = tuple(value for idx, value in enumerate(decoder_outputs) if idx != 1)
            encoder_outputs = tuple(value for idx, value in enumerate(encoder_outputs) if idx != 1)
            return decoder_outputs + encoder_outputs

        return Seq2SeqModelOutput(
            last_hidden_state=decoder_outputs.last_hidden_state,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )


class UdopForConditionalGeneration(UdopPreTrainedModel):
    _tied_weights_keys = [
        "encoder.embed_tokens.weight",
        "decoder.embed_tokens.weight",
        "encoder.embed_patches.proj.weight",
        "encoder.embed_patches.proj.bias",
        "encoder.relative_bias.biases.0.relative_attention_bias.weight",
        "decoder.relative_bias.biases.0.relative_attention_bias.weight",
        "lm_head.weight",
    ]

    def __init__(self, config):
        super(UdopForConditionalGeneration, self).__init__(config)

        # text and image embeddings
        self.shared = nn.Embedding(config.vocab_size, config.d_model)
        self.patch_embed = UdopPatchEmbeddings(config)

        encoder_config = deepcopy(config)
        encoder_config.is_decoder = False
        encoder_config.use_cache = False
        encoder_config.is_encoder_decoder = False
        self.encoder = UdopStack(encoder_config, self.shared, self.patch_embed)

        decoder_config = deepcopy(config)
        decoder_config.is_decoder = True
        decoder_config.is_encoder_decoder = False
        decoder_config.num_layers = config.num_decoder_layers
        self.decoder = UdopStack(decoder_config, self.shared)

        # The weights of the language modeling head are shared with those of the encoder and decoder
        self.lm_head = nn.Linear(config.d_model, config.vocab_size, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.shared

    def set_input_embeddings(self, new_embeddings):
        self.shared = new_embeddings
        self.encoder.set_input_embeddings(new_embeddings)
        self.decoder.set_input_embeddings(new_embeddings)

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def get_output_embeddings(self):
        return self.lm_head

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder

    def forward(
        self,
        input_ids: Tensor = None,
        attention_mask: Tensor = None,
        bbox: Dict[str, Any] = None,
        pixel_values: Optional[Tensor] = None,
        visual_bbox: Dict[str, Any] = None,
        decoder_input_ids: Optional[Tensor] = None,
        decoder_attention_mask: Optional[Tensor] = None,
        inputs_embeds: Optional[Tensor] = None,
        encoder_outputs: Optional[Tensor] = None,
        past_key_values: Optional[Tensor] = None,
        head_mask: Optional[Tensor] = None,
        decoder_inputs_embeds: Optional[Tensor] = None,
        decoder_head_mask: Optional[Tensor] = None,
        cross_attn_head_mask: Optional[Tensor] = None,
        use_cache=True,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        labels: Optional[Tensor] = None,
    ) -> Tuple[Tensor, ...]:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the language modeling loss. Indices should be in `[-100, 0, ..., config.vocab_size -
            1]`. All labels set to `-100` are ignored (masked), the loss is only computed for labels in `[0, ...,
            config.vocab_size]`.

        Returns:

        Examples:

        ```python
        >>> from transformers import AutoProcessor, UdopForConditionalGeneration
        >>> from datasets import load_dataset

        >>> # load model and processor
        >>> # in this case, we already have performed OCR ourselves
        >>> # so we initialize the processor with `apply_ocr=False`
        >>> processor = AutoProcessor.from_pretrained("microsoft/udop-large", apply_ocr=False)
        >>> model = UdopForConditionalGeneration.from_pretrained("microsoft/udop-large")

        >>> # load an example image, along with the words and coordinates
        >>> # which were extracted using an OCR engine
        >>> dataset = load_dataset("nielsr/funsd-layoutlmv3", split="train", trust_remote_code=True)
        >>> example = dataset[0]
        >>> image = example["image"]
        >>> words = example["tokens"]
        >>> boxes = example["bboxes"]

        >>> # one can use the various task prefixes (prompts) used during pre-training
        >>> # e.g. the task prefix for DocVQA is "Question answering. "
        >>> question = "Question answering. What is the date on the form?"
        >>> encoding = processor(image, question, words, boxes=boxes, return_tensors="ms")

        >>> # autoregressive generation
        >>> predicted_ids = model.generate(**encoding)
        >>> print(processor.batch_decode(predicted_ids, skip_special_tokens=True)[0])
        9/30/92
        ```"""

        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if decoder_input_ids is None and labels is not None:
            decoder_input_ids = self._shift_right(labels)

        # Encode if needed (training, first prediction pass)
        if encoder_outputs is None:
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                bbox=bbox,
                visual_bbox=visual_bbox,
                pixel_values=pixel_values,
                attention_mask=attention_mask,
                inputs_embeds=inputs_embeds,
                head_mask=head_mask,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )

        hidden_states = encoder_outputs[0]
        encoder_attention_mask = encoder_outputs.attention_mask if return_dict else encoder_outputs[1]

        # Decode
        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            inputs_embeds=decoder_inputs_embeds,
            past_key_values=past_key_values,
            encoder_hidden_states=hidden_states,
            encoder_attention_mask=encoder_attention_mask,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = decoder_outputs[0]

        if self.config.tie_word_embeddings:
            # Rescale output before projecting on vocab
            # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/transformer/transformer.py#L586
            sequence_output = sequence_output * (self.config.d_model**-0.5)

        lm_logits = self.lm_head(sequence_output)

        loss = None
        if labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-100)
            loss = loss_fct(lm_logits.view(-1, lm_logits.shape[-1]), labels.view(-1))

        if not return_dict:
            output = (lm_logits,) + decoder_outputs[2:] + (encoder_outputs[0],) + encoder_outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return Seq2SeqLMOutput(
            loss=loss,
            logits=lm_logits,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )

    def prepare_inputs_for_generation(
        self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        use_cache=None,
        encoder_outputs=None,
        **kwargs,
    ):
        # cut decoder_input_ids if past is used
        if past_key_values is not None:
            input_ids = input_ids[:, -1:]

        return {
            "decoder_input_ids": input_ids,
            "past_key_values": past_key_values,
            "encoder_outputs": encoder_outputs,
            "attention_mask": attention_mask,
            "head_mask": head_mask,
            "decoder_head_mask": decoder_head_mask,
            "cross_attn_head_mask": cross_attn_head_mask,
            "use_cache": use_cache,
            "bbox": kwargs.get("bbox", None),
            "pixel_values": kwargs.get("pixel_values", None),
            "visual_bbox": kwargs.get("visual_bbox", None),
        }

    # Copied from transformers.models.t5.modeling_t5.T5ForConditionalGeneration._reorder_cache
    def _reorder_cache(self, past_key_values, beam_idx):
        # if decoder past is not included in output
        # speedy decoding is disabled and no need to reorder
        if past_key_values is None:
            logger.warning("You might want to consider setting `use_cache=True` to speed up decoding")
            return past_key_values

        reordered_decoder_past = ()
        for layer_past_states in past_key_values:
            # get the correct batch idx from layer past batch dim
            # batch dim of `past` is at 2nd position
            reordered_layer_past_states = ()
            for layer_past_state in layer_past_states:
                # need to set correct `past` for each of the four key / value states
                reordered_layer_past_states = reordered_layer_past_states + (
                    layer_past_state.index_select(0, beam_idx),
                )

            if reordered_layer_past_states[0].shape != layer_past_states[0].shape:
                raise ValueError(
                    f"reordered_layer_past_states[0] shape {reordered_layer_past_states[0].shape} and layer_past_states[0] shape {layer_past_states[0].shape} mismatched"
                )
            if len(reordered_layer_past_states) != len(layer_past_states):
                raise ValueError(
                    f"length of reordered_layer_past_states {len(reordered_layer_past_states)} and length of layer_past_states {len(layer_past_states)} mismatched"
                )

            reordered_decoder_past = reordered_decoder_past + (reordered_layer_past_states,)
        return reordered_decoder_past


class UdopEncoderModel(UdopPreTrainedModel):
    _tied_weights_keys = [
        "encoder.embed_tokens.weight",
        "encoder.embed_patches.proj.weight",
        "encoder.embed_patches.proj.bias",
        "encoder.relative_bias.biases.0.relative_attention_bias.weight",
    ]

    def __init__(self, config: UdopConfig):
        super().__init__(config)

        # text and image embeddings
        self.shared = nn.Embedding(config.vocab_size, config.d_model)
        self.patch_embed = UdopPatchEmbeddings(config)

        encoder_config = deepcopy(config)
        encoder_config.is_decoder = False
        encoder_config.use_cache = False
        encoder_config.is_encoder_decoder = False
        self.encoder = UdopStack(encoder_config, self.shared, self.patch_embed)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.shared

    def set_input_embeddings(self, new_embeddings):
        self.shared = new_embeddings
        self.encoder.set_input_embeddings(new_embeddings)

    def get_encoder(self):
        return self.encoder

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.block[layer].layer[0].SelfAttention.prune_heads(heads)

    def forward(
        self,
        input_ids: Tensor = None,
        bbox: Dict[str, Any] = None,
        attention_mask: Tensor = None,
        pixel_values: Optional[Tensor] = None,
        visual_bbox: Dict[str, Any] = None,
        head_mask: Optional[Tensor] = None,
        inputs_embeds: Optional[Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple[mindspore.Tensor], BaseModelOutputWithAttentionMask]:
        r"""
        Returns:

        Example:

        ```python
        >>> from transformers import AutoProcessor, UdopEncoderModel
        >>> from huggingface_hub import hf_hub_download
        >>> from datasets import load_dataset

        >>> # load model and processor
        >>> # in this case, we already have performed OCR ourselves
        >>> # so we initialize the processor with `apply_ocr=False`
        >>> processor = AutoProcessor.from_pretrained("microsoft/udop-large", apply_ocr=False)
        >>> model = UdopEncoderModel.from_pretrained("microsoft/udop-large")

        >>> # load an example image, along with the words and coordinates
        >>> # which were extracted using an OCR engine
        >>> dataset = load_dataset("nielsr/funsd-layoutlmv3", split="train", trust_remote_code=True)
        >>> example = dataset[0]
        >>> image = example["image"]
        >>> words = example["tokens"]
        >>> boxes = example["bboxes"]
        >>> encoding = processor(image, words, boxes=boxes, return_tensors="ms")

        >>> outputs = model(**encoding)
        >>> last_hidden_states = outputs.last_hidden_state
        ```"""
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        encoder_outputs = self.encoder(
            input_ids=input_ids,
            bbox=bbox,
            visual_bbox=visual_bbox,
            pixel_values=pixel_values,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        return encoder_outputs

__all__ = [
    "UdopForConditionalGeneration",
    "UdopPreTrainedModel",
    "UdopModel",
    "UdopEncoderModel"
]
