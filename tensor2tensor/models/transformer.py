# coding=utf-8
# Copyright 2017 The Tensor2Tensor Authors.
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

"""transformer (attention).

encoder: [Self-Attention, Feed-forward] x n
decoder: [Self-Attention, Source-Target-Attention, Feed-forward] x n
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from six.moves import xrange  # pylint: disable=redefined-builtin

from tensor2tensor.layers import common_attention
from tensor2tensor.layers import common_hparams
from tensor2tensor.layers import common_layers
from tensor2tensor.utils import expert_utils
from tensor2tensor.utils import registry
from tensor2tensor.utils import t2t_model

import tensorflow as tf


@registry.register_model
class Transformer(t2t_model.T2TModel):
  """Attention net.  See file docstring."""

  def encode(self, inputs, target_space, hparams):
    """Encode transformer inputs.

    Args:
      inputs: Transformer inputs [batch_size, input_length, hidden_dim]
      target_space: scalar, target space ID.
      hparams: hyperparmeters for model.

    Returns:
      Tuple of:
          encoder_output: Encoder representation.
              [batch_size, input_length, hidden_dim]
          encoder_decoder_attention_bias: Bias and mask weights for
              encodre-decoder attention. [batch_size, input_length]
    """
    inputs = common_layers.flatten4d3d(inputs)

    encoder_input, self_attention_bias, encoder_decoder_attention_bias = (
        transformer_prepare_encoder(inputs, target_space, hparams))

    encoder_input = tf.nn.dropout(
        encoder_input, 1.0 - hparams.layer_prepostprocess_dropout)

    encoder_output = transformer_encoder(
        encoder_input,
        self_attention_bias,
        hparams)

    return encoder_output, encoder_decoder_attention_bias

  def decode(
      self,
      decoder_input,
      encoder_output,
      encoder_decoder_attention_bias,
      decoder_self_attention_bias,
      hparams,
      cache=None):
    """Decode Transformer outputs from encoder representation.

    Args:
      decoder_input: inputs to bottom of the model.
          [batch_size, decoder_length, hidden_dim]
      encoder_output: Encoder representation.
          [batch_size, input_length, hidden_dim]
      encoder_decoder_attention_bias: Bias and mask weights for
          encoder-decoder attention. [batch_size, input_length]
      decoder_self_attention_bias: Bias and mask weights for decoder
          self-attention. [batch_size, decoder_length]
      hparams: hyperparmeters for model.
      cache: dict, containing tensors which are the results of previous
          attentions, used for fast decoding.

    Returns:
      Final decoder representaiton. [batch_size, decoder_length, hidden_dim]
    """
    decoder_input = tf.nn.dropout(decoder_input,
                                  1.0 - hparams.layer_prepostprocess_dropout)

    decoder_output = transformer_decoder(
        decoder_input,
        encoder_output,
        decoder_self_attention_bias,
        encoder_decoder_attention_bias,
        hparams,
        cache=cache)

    # Expand since t2t expects 4d tensors.
    return tf.expand_dims(decoder_output, axis=2)

  def model_fn_body(self, features):
    """Transformet main model_fn.

    Args:
      features: Map of features to the model. Should contain the following:
          "inputs": Transformer inputs [batch_size, input_length, hidden_dim]
          "tragets": Target decoder outputs.
              [batch_size, decoder_length, hidden_dim]
          "target_space_id"

    Returns:
      Final decoder representaiton. [batch_size, decoder_length, hidden_dim]
    """
    hparams = self._hparams

    inputs = features["inputs"]

    target_space = features["target_space_id"]
    encoder_output, encoder_decoder_attention_bias = self.encode(
        inputs, target_space, hparams)

    targets = features["targets"]
    targets = common_layers.flatten4d3d(targets)

    decoder_input, decoder_self_attention_bias = transformer_prepare_decoder(
        targets, hparams)

    return self.decode(
        decoder_input,
        encoder_output,
        encoder_decoder_attention_bias,
        decoder_self_attention_bias,
        hparams)

  def _greedy_infer(
      self, features, decode_length, last_position_only=True):
    """Fast version of greedy decoding.

    Args:
      features: an map of string to `Tensor`
      decode_length: an integer.  How many additional timesteps to decode.
      last_position_only: MUST be true for fast decoding!

    Returns:
       samples: [batch_size, input_length + decode_length]
       logits: Not returned
       losses: Not returned

    Raises:
      ValueError: If last_position_only if False
      NotImplementedError: If there are multiple data shards.
    """
    if not last_position_only:
      raise ValueError("Fast decoding only deals with the last positions!")
    if self._num_datashards != 1:
      raise NotImplementedError("Fast decoding only supports a single shard.")
    dp = self._data_parallelism
    hparams = self._hparams

    inputs = features["inputs"]
    batch_size = tf.shape(inputs)[0]
    target_modality = self._problem_hparams.target_modality
    if t2t_model.is_class_modality(target_modality):
      decode_length = 1
    else:
      decode_length = tf.shape(inputs)[1] + decode_length

    # TODO(llion): Clean up this reshaping logic.
    inputs = tf.expand_dims(inputs, axis=1)
    if len(inputs.shape) < 5:
      inputs = tf.expand_dims(inputs, axis=4)
    s = tf.shape(inputs)
    inputs = tf.reshape(inputs, [s[0] * s[1], s[2], s[3], s[4]])
    # _shard_features called to ensure that the variable names match
    inputs = self._shard_features({"inputs": inputs})["inputs"]
    input_modality = self._problem_hparams.input_modality["inputs"]
    with tf.variable_scope(input_modality.name):
      inputs = input_modality.bottom_sharded(inputs, dp)
    with tf.variable_scope("body"):
      encoder_output, encoder_decoder_attention_bias = dp(
          self.encode, inputs, features["target_space_id"], hparams)

    if hparams.pos == "timing":
      timing_signal = common_attention.get_timing_signal_1d(
          decode_length + 1, hparams.hidden_size)

    def preprocess_targets(targets, i):
      """Performs preprocessing steps on the targets to prepare for the decoder.

      This includes:
        - Embedding the ids.
        - Flattening to 3D tensor.
        - Optionally adding timing signals.

      Args:
        targets: inputs ids to the decoder. [batch_size, 1]
        i: scalar, Step number of the decoding loop.

      Returns:
        Processed targets [batch_size, 1, hidden_dim]
      """
      # _shard_features called to ensure that the variable names match
      targets = self._shard_features({"targets": targets})["targets"]
      with tf.variable_scope(target_modality.name):
        targets = target_modality.targets_bottom_sharded(targets, dp)[0]
      targets = common_layers.flatten4d3d(targets)

      # TODO(llion): Explain! Is this even needed?
      targets = tf.cond(
          tf.equal(i, 0),
          lambda: tf.zeros_like(targets),
          lambda: targets)

      if hparams.pos == "timing":
        targets += timing_signal[:, i:i+1]
      return targets

    decoder_self_attention_bias = (
        common_attention.attention_bias_lower_triangle(decode_length))
    if hparams.proximity_bias:
      decoder_self_attention_bias += common_attention.attention_bias_proximal(
          decode_length)

    def symbols_to_logits_fn(ids, i, cache):
      """Go from ids to logits for next symbol."""
      targets = tf.expand_dims(tf.expand_dims(ids, axis=2), axis=3)
      targets = preprocess_targets(targets, i)

      bias = decoder_self_attention_bias[:, :, i:i+1, :i+1]

      with tf.variable_scope("body"):
        body_outputs = dp(
            self.decode,
            targets,
            encoder_output[0],
            encoder_decoder_attention_bias[0],
            bias,
            hparams,
            cache)

      with tf.variable_scope(target_modality.name):
        logits = target_modality.top_sharded(body_outputs, None, dp)[0]

      return tf.squeeze(logits, axis=[1, 2, 3])

    def inner_loop(i, next_id, decoded_ids, cache):
      logits = symbols_to_logits_fn(next_id, i, cache)
      next_id = tf.expand_dims(tf.argmax(logits, axis=-1), axis=1)
      decoded_ids = tf.concat([decoded_ids, next_id], axis=1)
      return i+1, next_id, decoded_ids, cache

    key_channels = hparams.attention_key_channels or hparams.hidden_size
    value_channels = hparams.attention_value_channels or hparams.hidden_size
    num_layers = hparams.num_decoder_layers or hparams.num_hidden_layers

    cache = {
        "layer_%d" % layer: {
            "k": tf.zeros([batch_size, 0, key_channels]),
            "v": tf.zeros([batch_size, 0, value_channels]),
        } for layer in range(num_layers)
    }
    decoded_ids = tf.zeros([batch_size, 0], dtype=tf.int64)
    next_id = tf.zeros([batch_size, 1], dtype=tf.int64)
    _, _, decoded_ids, _ = tf.while_loop(
        # TODO(llion): Early stopping.
        lambda i, *_: tf.less(i, decode_length),
        inner_loop,
        [tf.constant(0), next_id, decoded_ids, cache],
        shape_invariants=[
            tf.TensorShape([]),
            tf.TensorShape([None, None]),
            tf.TensorShape([None, None]),
            {"layer_%d" % layer: {
                "k": tf.TensorShape([None, None, key_channels]),
                "v": tf.TensorShape([None, None, value_channels]),
            } for layer in range(num_layers)}
        ])

    return decoded_ids, None, None


@registry.register_model
class TransformerEncoder(t2t_model.T2TModel):
  """Transformer, encoder only."""

  def model_fn_body(self, features):
    hparams = self._hparams
    inputs = features["inputs"]
    target_space = features["target_space_id"]

    inputs = common_layers.flatten4d3d(inputs)

    (encoder_input, encoder_self_attention_bias,
     _) = (transformer_prepare_encoder(inputs, target_space, hparams))

    encoder_input = tf.nn.dropout(encoder_input,
                                  1.0 - hparams.layer_prepostprocess_dropout)
    encoder_output = transformer_encoder(encoder_input,
                                         encoder_self_attention_bias, hparams)
    encoder_output = tf.expand_dims(encoder_output, 2)

    return encoder_output


@registry.register_model
class TransformerDecoder(t2t_model.T2TModel):
  """Transformer, decoder only."""

  def model_fn_body(self, features):
    hparams = self._hparams
    targets = features["targets"]

    targets = common_layers.flatten4d3d(targets)

    (decoder_input, decoder_self_attention_bias) = transformer_prepare_decoder(
        targets, hparams)

    decoder_input = tf.nn.dropout(decoder_input,
                                  1.0 - hparams.layer_prepostprocess_dropout)

    decoder_output = transformer_decoder(
        decoder_input, None, decoder_self_attention_bias, None, hparams)
    decoder_output = tf.expand_dims(decoder_output, 2)

    return decoder_output


def transformer_prepare_encoder(inputs, target_space, hparams):
  """Prepare one shard of the model for the encoder.

  Args:
    inputs: a Tensor.
    target_space: a Tensor.
    hparams: run hyperparameters

  Returns:
    encoder_input: a Tensor, bottom of encoder stack
    encoder_self_attention_bias: a bias tensor for use in encoder self-attention
    encoder_decoder_attention_bias: a bias tensor for use in encoder-decoder
      attention
  """
  ishape_static = inputs.shape.as_list()
  encoder_input = inputs
  encoder_padding = common_attention.embedding_to_padding(encoder_input)
  ignore_padding = common_attention.attention_bias_ignore_padding(
      encoder_padding)
  encoder_self_attention_bias = ignore_padding
  encoder_decoder_attention_bias = ignore_padding
  if hparams.proximity_bias:
    encoder_self_attention_bias += common_attention.attention_bias_proximal(
        tf.shape(inputs)[1])
  # Append target_space_id embedding to inputs.
  emb_target_space = common_layers.embedding(
      target_space, 32, ishape_static[-1], name="target_space_embedding")
  emb_target_space = tf.reshape(emb_target_space, [1, 1, -1])
  encoder_input += emb_target_space
  if hparams.pos == "timing":
    encoder_input = common_attention.add_timing_signal_1d(encoder_input)
  return (encoder_input, encoder_self_attention_bias,
          encoder_decoder_attention_bias)


def transformer_prepare_decoder(targets, hparams):
  """Prepare one shard of the model for the decoder.

  Args:
    targets: a Tensor.
    hparams: run hyperparameters

  Returns:
    decoder_input: a Tensor, bottom of decoder stack
    decoder_self_attention_bias: a bias tensor for use in encoder self-attention
  """
  decoder_self_attention_bias = (
      common_attention.attention_bias_lower_triangle(tf.shape(targets)[1]))
  if hparams.proximity_bias:
    decoder_self_attention_bias += common_attention.attention_bias_proximal(
        tf.shape(targets)[1])
  decoder_input = common_layers.shift_right_3d(targets)
  if hparams.pos == "timing":
    decoder_input = common_attention.add_timing_signal_1d(decoder_input)
  return (decoder_input, decoder_self_attention_bias)


def transformer_encoder(encoder_input,
                        encoder_self_attention_bias,
                        hparams,
                        name="encoder"):
  """A stack of transformer layers.

  Args:
    encoder_input: a Tensor
    encoder_self_attention_bias: bias Tensor for self-attention
       (see common_attention.attention_bias())
    hparams: hyperparameters for model
    name: a string

  Returns:
    y: a Tensors
  """
  x = encoder_input
  with tf.variable_scope(name):
    pad_remover = None
    if hparams.use_pad_remover:
      pad_remover = expert_utils.PadRemover(
          common_attention.attention_bias_to_padding(
              encoder_self_attention_bias))
    for layer in xrange(hparams.num_encoder_layers or
                        hparams.num_hidden_layers):
      with tf.variable_scope("layer_%d" % layer):
        with tf.variable_scope("self_attention"):
          y = common_attention.multihead_attention(
              common_layers.layer_preprocess(x, hparams),
              None,
              encoder_self_attention_bias,
              hparams.attention_key_channels or hparams.hidden_size,
              hparams.attention_value_channels or hparams.hidden_size,
              hparams.hidden_size,
              hparams.num_heads,
              hparams.attention_dropout,
              attention_type=hparams.self_attention_type,
              max_relative_position=hparams.max_relative_position)
          x = common_layers.layer_postprocess(x, y, hparams)
        with tf.variable_scope("ffn"):
          y = transformer_ffn_layer(
              common_layers.layer_preprocess(x, hparams), hparams, pad_remover)
          x = common_layers.layer_postprocess(x, y, hparams)
    # if normalization is done in layer_preprocess, then it shuold also be done
    # on the output, since the output can grow very large, being the sum of
    # a whole stack of unnormalized layer outputs.
    return common_layers.layer_preprocess(x, hparams)


def transformer_decoder(decoder_input,
                        encoder_output,
                        decoder_self_attention_bias,
                        encoder_decoder_attention_bias,
                        hparams,
                        cache=None,
                        name="decoder"):
  """A stack of transformer layers.

  Args:
    decoder_input: a Tensor
    encoder_output: a Tensor
    decoder_self_attention_bias: bias Tensor for self-attention
      (see common_attention.attention_bias())
    encoder_decoder_attention_bias: bias Tensor for encoder-decoder attention
      (see common_attention.attention_bias())
    hparams: hyperparameters for model
    cache: dict, containing tensors which are the results of previous
        attentions, used for fast decoding.
    name: a string

  Returns:
    y: a Tensors
  """
  x = decoder_input
  with tf.variable_scope(name):
    for layer in xrange(hparams.num_decoder_layers or
                        hparams.num_hidden_layers):
      layer_name = "layer_%d" % layer
      layer_cache = cache[layer_name] if cache is not None else None
      with tf.variable_scope(layer_name):
        with tf.variable_scope("self_attention"):
          y = common_attention.multihead_attention(
              common_layers.layer_preprocess(x, hparams),
              None,
              decoder_self_attention_bias,
              hparams.attention_key_channels or hparams.hidden_size,
              hparams.attention_value_channels or hparams.hidden_size,
              hparams.hidden_size,
              hparams.num_heads,
              hparams.attention_dropout,
              attention_type=hparams.self_attention_type,
              max_relative_position=hparams.max_relative_position,
              cache=layer_cache)
          x = common_layers.layer_postprocess(x, y, hparams)
        if encoder_output is not None:
          with tf.variable_scope("encdec_attention"):
            # TODO(llion): Add caching.
            y = common_attention.multihead_attention(
                common_layers.layer_preprocess(x, hparams),
                encoder_output,
                encoder_decoder_attention_bias,
                hparams.attention_key_channels or hparams.hidden_size,
                hparams.attention_value_channels or hparams.hidden_size,
                hparams.hidden_size, hparams.num_heads,
                hparams.attention_dropout)
            x = common_layers.layer_postprocess(x, y, hparams)
        with tf.variable_scope("ffn"):
          y = transformer_ffn_layer(
              common_layers.layer_preprocess(x, hparams), hparams)
          x = common_layers.layer_postprocess(x, y, hparams)
    # if normalization is done in layer_preprocess, then it shuold also be done
    # on the output, since the output can grow very large, being the sum of
    # a whole stack of unnormalized layer outputs.
    return common_layers.layer_preprocess(x, hparams)

### TODO(sanqiang) Our Ideas!
def transformer_decoder_attngate(decoder_input,
                      encoder_output,
                      decoder_self_attention_bias,
                      encoder_decoder_attention_bias,
                      hparams,
                      cache=None,
                      name="decoder"):
  """A stack of transformer layers.

  Args:
    decoder_input: a Tensor
    encoder_output: a Tensor
    decoder_self_attention_bias: bias Tensor for self-attention
      (see common_attention.attention_bias())
    encoder_decoder_attention_bias: bias Tensor for encoder-decoder attention
      (see common_attention.attention_bias())
    hparams: hyperparameters for model
    cache: dict, containing tensors which are the results of previous
        attentions, used for fast decoding.
    name: a string

  Returns:
    y: a Tensors
  """
  x = decoder_input
  with tf.variable_scope(name):
      for layer in xrange(hparams.num_decoder_layers or
                                  hparams.num_hidden_layers):
          layer_name = "layer_%d" % layer
          layer_cache = cache[layer_name] if cache is not None else None
          with tf.variable_scope(layer_name):
              with tf.variable_scope("self_attention"):
                  y = common_attention.multihead_attention(
                      common_layers.layer_preprocess(x, hparams),
                      None,
                      decoder_self_attention_bias,
                      hparams.attention_key_channels or hparams.hidden_size,
                      hparams.attention_value_channels or hparams.hidden_size,
                      hparams.hidden_size,
                      hparams.num_heads,
                      hparams.attention_dropout,
                      attention_type=hparams.self_attention_type,
                      max_relative_position=hparams.max_relative_position,
                      cache=layer_cache)
                  x_self_attn = common_layers.layer_postprocess(x, y, hparams)
              with tf.variable_scope("encdec_attention"):
                  # TODO(llion): Add caching.
                  y = common_attention.multihead_attention(
                      common_layers.layer_preprocess(x_self_attn, hparams),
                      encoder_output,
                      encoder_decoder_attention_bias,
                      hparams.attention_key_channels or hparams.hidden_size,
                      hparams.attention_value_channels or hparams.hidden_size,
                      hparams.hidden_size, hparams.num_heads,
                      hparams.attention_dropout)
                  x_decode_attn = common_layers.layer_postprocess(x_self_attn, y, hparams)

                  evidence = tf.concat([x_self_attn, x_decode_attn], axis=2)

                  # Gated Self Attention
                  gate_selfattn_filter = tf.get_variable(
                      'gate_selfattn',
                      [1, hparams.hidden_size * 2, hparams.hidden_size],
                      tf.float32, initializer=tf.contrib.layers.xavier_initializer())
                  gate_selfattn = tf.tanh(
                      tf.nn.conv1d(evidence, gate_selfattn_filter, 1, 'SAME'))
                  x_self_attn *= gate_selfattn

                  # Gated Decoded Attention
                  gate_decfattn_filter = tf.get_variable(
                      'gate_decattn',
                      [1, hparams.hidden_size * 2, hparams.hidden_size],
                      tf.float32, initializer=tf.contrib.layers.xavier_initializer())
                  gate_decfattn = tf.tanh(
                      tf.nn.conv1d(evidence, gate_decfattn_filter, 1, 'SAME'))
                  x_decode_attn *= gate_decfattn

                  # Output combined attention
                  x = x_self_attn + x_decode_attn
              with tf.variable_scope("ffn"):
                  y = transformer_ffn_layer(
                      common_layers.layer_preprocess(x, hparams), hparams)
                  x = common_layers.layer_postprocess(x, y, hparams)
      # if normalization is done in layer_preprocess, then it shuold also be done
      # on the output, since the output can grow very large, being the sum of
      # a whole stack of unnormalized layer outputs.
      return common_layers.layer_preprocess(x, hparams)


def transformer_encoder_gate(encoder_input,
                             encoder_self_attention_bias,
                             hparams,
                             name="encoder"):
    """A stack of transformer layers.

    Args:
      encoder_input: a Tensor
      encoder_self_attention_bias: bias Tensor for self-attention
         (see common_attention.attention_bias())
      hparams: hyperparameters for model
      name: a string

    Returns:
      y: a Tensors
    """
    x = encoder_input
    with tf.variable_scope(name):
        pad_remover = None
        if hparams.use_pad_remover:
            pad_remover = expert_utils.PadRemover(
                common_attention.attention_bias_to_padding(
                    encoder_self_attention_bias))
        for layer in xrange(hparams.num_encoder_layers or
                                    hparams.num_hidden_layers):
            with tf.variable_scope("layer_%d" % layer):
                with tf.variable_scope("self_attention"):
                    y = common_attention.multihead_attention(
                        common_layers.layer_preprocess(x, hparams),
                        None,
                        encoder_self_attention_bias,
                        hparams.attention_key_channels or hparams.hidden_size,
                        hparams.attention_value_channels or hparams.hidden_size,
                        hparams.hidden_size,
                        hparams.num_heads,
                        hparams.attention_dropout,
                        attention_type=hparams.self_attention_type,
                        max_relative_position=hparams.max_relative_position)
                    x = common_layers.layer_postprocess(x, y, hparams)

                    gate_fiter = tf.get_variable(
                        'gate_layer_%d' % layer,
                        [1, hparams.hidden_size, hparams.hidden_size],
                        tf.float32, initializer=tf.contrib.layers.xavier_initializer())
                    gate_x = tf.tanh(
                        tf.nn.conv1d(x, gate_fiter, 1, 'SAME'))
                    x *= gate_x

                with tf.variable_scope("ffn"):
                    y = transformer_ffn_layer(
                        common_layers.layer_preprocess(x, hparams), hparams, pad_remover)
                    x = common_layers.layer_postprocess(x, y, hparams)

                    gate_fiter = tf.get_variable(
                        'gate_layer_%d' % layer,
                        [1, hparams.hidden_size, hparams.hidden_size],
                        tf.float32, initializer=tf.contrib.layers.xavier_initializer())
                    gate_x = tf.tanh(
                        tf.nn.conv1d(x, gate_fiter, 1, 'SAME'))
                    x *= gate_x
        # if normalization is done in layer_preprocess, then it shuold also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.
        return common_layers.layer_preprocess(x, hparams)


def transformer_decoder_gate(decoder_input,
                        encoder_output,
                        decoder_self_attention_bias,
                        encoder_decoder_attention_bias,
                        hparams,
                        cache=None,
                        name="decoder"):
  """A stack of transformer layers.

  Args:
    decoder_input: a Tensor
    encoder_output: a Tensor
    decoder_self_attention_bias: bias Tensor for self-attention
      (see common_attention.attention_bias())
    encoder_decoder_attention_bias: bias Tensor for encoder-decoder attention
      (see common_attention.attention_bias())
    hparams: hyperparameters for model
    cache: dict, containing tensors which are the results of previous
        attentions, used for fast decoding.
    name: a string

  Returns:
    y: a Tensors
  """
  x = decoder_input
  with tf.variable_scope(name):
    for layer in xrange(hparams.num_decoder_layers or
                        hparams.num_hidden_layers):
      layer_name = "layer_%d" % layer
      layer_cache = cache[layer_name] if cache is not None else None
      with tf.variable_scope(layer_name):
        with tf.variable_scope("self_attention"):
          y = common_attention.multihead_attention(
              common_layers.layer_preprocess(x, hparams),
              None,
              decoder_self_attention_bias,
              hparams.attention_key_channels or hparams.hidden_size,
              hparams.attention_value_channels or hparams.hidden_size,
              hparams.hidden_size,
              hparams.num_heads,
              hparams.attention_dropout,
              attention_type=hparams.self_attention_type,
              max_relative_position=hparams.max_relative_position,
              cache=layer_cache)
          x = common_layers.layer_postprocess(x, y, hparams)

          gate_fiter = tf.get_variable(
              'gate_layer_%d' % layer,
              [1, hparams.hidden_size, hparams.hidden_size],
              tf.float32, initializer=tf.contrib.layers.xavier_initializer())
          gate_x = tf.tanh(
              tf.nn.conv1d(x, gate_fiter, 1, 'SAME'))
          x *= gate_x

        if encoder_output is not None:
          with tf.variable_scope("encdec_attention"):
            # TODO(llion): Add caching.
            y = common_attention.multihead_attention(
                common_layers.layer_preprocess(x, hparams),
                encoder_output,
                encoder_decoder_attention_bias,
                hparams.attention_key_channels or hparams.hidden_size,
                hparams.attention_value_channels or hparams.hidden_size,
                hparams.hidden_size, hparams.num_heads,
                hparams.attention_dropout)
            x = common_layers.layer_postprocess(x, y, hparams)

            gate_fiter = tf.get_variable(
                'gate_layer_%d' % layer,
                [1, hparams.hidden_size, hparams.hidden_size],
                tf.float32, initializer=tf.contrib.layers.xavier_initializer())
            gate_x = tf.tanh(
                tf.nn.conv1d(x, gate_fiter, 1, 'SAME'))
            x *= gate_x
        with tf.variable_scope("ffn"):
          y = transformer_ffn_layer(
              common_layers.layer_preprocess(x, hparams), hparams)
          x = common_layers.layer_postprocess(x, y, hparams)

          gate_fiter = tf.get_variable(
              'gate_layer_%d' % layer,
              [1, hparams.hidden_size, hparams.hidden_size],
              tf.float32, initializer=tf.contrib.layers.xavier_initializer())
          gate_x = tf.tanh(
              tf.nn.conv1d(x, gate_fiter, 1, 'SAME'))
        x *= gate_x
    # if normalization is done in layer_preprocess, then it shuold also be done
    # on the output, since the output can grow very large, being the sum of
    # a whole stack of unnormalized layer outputs.
    return common_layers.layer_preprocess(x, hparams)


def transformer_ffn_layer(x, hparams, pad_remover=None):
  """Feed-forward layer in the transformer.

  Args:
    x: a Tensor of shape [batch_size, length, hparams.hidden_size]
    hparams: hyperparmeters for model
    pad_remover: an expert_utils.PadRemover object tracking the padding
      positions. If provided, when using convolutional settings, the padding
      is removed before applying the convolution, and restored afterward. This
      can give a significant speedup.

  Returns:
    a Tensor of shape [batch_size, length, hparams.hidden_size]
  """
  if hparams.ffn_layer == "conv_hidden_relu":
    # In simple convolution mode, use `pad_remover` to speed up processing.
    if pad_remover:
      original_shape = tf.shape(x)
      # Collapse `x` across examples, and remove padding positions.
      x = tf.reshape(x, tf.concat([[-1], tf.shape(x)[2:]], axis=0))
      x = tf.expand_dims(pad_remover.remove(x), axis=0)
    conv_output = common_layers.conv_hidden_relu(
        x,
        hparams.filter_size,
        hparams.hidden_size,
        dropout=hparams.relu_dropout)
    if pad_remover:
      # Restore `conv_output` to the original shape of `x`, including padding.
      conv_output = tf.reshape(
          pad_remover.restore(tf.squeeze(conv_output, axis=0)), original_shape)
    return conv_output
  elif hparams.ffn_layer == "parameter_attention":
    return common_attention.parameter_attention(
        x, hparams.parameter_attention_key_channels or hparams.hidden_size,
        hparams.parameter_attention_value_channels or hparams.hidden_size,
        hparams.hidden_size, hparams.filter_size, hparams.num_heads,
        hparams.attention_dropout)
  elif hparams.ffn_layer == "conv_hidden_relu_with_sepconv":
    return common_layers.conv_hidden_relu(
        x,
        hparams.filter_size,
        hparams.hidden_size,
        kernel_size=(3, 1),
        second_kernel_size=(31, 1),
        padding="LEFT",
        dropout=hparams.relu_dropout)
  else:
    assert hparams.ffn_layer == "none"
    return x


@registry.register_hparams
def transformer_base():
  """Set of hyperparameters."""
  hparams = common_hparams.basic_params1()
  hparams.norm_type = "layer"
  hparams.hidden_size = 512
  hparams.batch_size = 4096
  hparams.max_length = 256
  hparams.dropout = 0.0
  hparams.clip_grad_norm = 0.  # i.e. no gradient clipping
  hparams.optimizer_adam_epsilon = 1e-9
  hparams.learning_rate_decay_scheme = "noam"
  hparams.learning_rate = 0.1
  hparams.learning_rate_warmup_steps = 4000
  hparams.initializer_gain = 1.0
  hparams.num_hidden_layers = 6
  hparams.initializer = "uniform_unit_scaling"
  hparams.weight_decay = 0.0
  hparams.optimizer_adam_beta1 = 0.9
  hparams.optimizer_adam_beta2 = 0.98
  hparams.num_sampled_classes = 0
  hparams.label_smoothing = 0.1
  hparams.shared_embedding_and_softmax_weights = int(True)

  # Add new ones like this.
  hparams.add_hparam("filter_size", 2048)
  # Layer-related flags. If zero, these fall back on hparams.num_hidden_layers.
  hparams.add_hparam("num_encoder_layers", 0)
  hparams.add_hparam("num_decoder_layers", 0)
  # Attention-related flags.
  hparams.add_hparam("num_heads", 8)
  hparams.add_hparam("attention_key_channels", 0)
  hparams.add_hparam("attention_value_channels", 0)
  hparams.add_hparam("ffn_layer", "conv_hidden_relu")
  hparams.add_hparam("parameter_attention_key_channels", 0)
  hparams.add_hparam("parameter_attention_value_channels", 0)
  # All hyperparameters ending in "dropout" are automatically set to 0.0
  # when not in training mode.
  hparams.add_hparam("attention_dropout", 0.0)
  hparams.add_hparam("relu_dropout", 0.0)
  hparams.add_hparam("pos", "timing")  # timing, none
  hparams.add_hparam("nbr_decoder_problems", 1)
  hparams.add_hparam("proximity_bias", int(False))
  hparams.add_hparam("use_pad_remover", int(True))
  hparams.add_hparam("self_attention_type", "dot_product")
  hparams.add_hparam("max_relative_position", 0)
  return hparams


@registry.register_hparams
def transformer_n_da():
  """Normalize on layer input, instead of after residual connection.

  This version seems to cure failure-to-learn bugs - for example, with very
  deep networks or hard-to-learn mappings.

  Probably this should become the default.

  Returns:
    a hyperparameters.
  """
  hparams = transformer_base()
  hparams.layer_preprocess_sequence = "n"
  hparams.layer_postprocess_sequence = "da"
  # This version seems to benefit from a higher learning rate.
  hparams.learning_rate = 0.4
  return hparams


@registry.register_hparams
def transformer_n_da_l10():
  hparams = transformer_n_da()
  hparams.num_hidden_layers = 10
  return hparams


@registry.register_hparams
def transformer_big():
  """HParams for transfomer big model on WMT."""
  hparams = transformer_base()
  hparams.hidden_size = 1024
  hparams.filter_size = 4096
  hparams.num_heads = 16
  hparams.layer_prepostprocess_dropout = 0.3
  return hparams


@registry.register_hparams
def transformer_big_single_gpu():
  """HParams for transformer big model for single gpu."""
  hparams = transformer_big()
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.learning_rate_warmup_steps = 16000
  hparams.optimizer_adam_beta2 = 0.998
  return hparams


@registry.register_hparams
def transformer_base_single_gpu():
  """HParams for transformer base model for single gpu."""
  hparams = transformer_base()
  hparams.batch_size = 2048
  hparams.learning_rate_warmup_steps = 16000
  return hparams


@registry.register_hparams
def transformer_parsing_base():
  """Hparams for parsing on wsj only."""
  hparams = transformer_base()
  hparams.attention_dropout = 0.2
  hparams.layer_prepostprocess_dropout = 0.2
  hparams.max_length = 512
  hparams.learning_rate_warmup_steps = 16000
  hparams.hidden_size = 1024
  hparams.learning_rate = 0.05
  hparams.shared_embedding_and_softmax_weights = int(False)
  return hparams


@registry.register_hparams
def transformer_parsing_big():
  """HParams for parsing on wsj semi-supervised."""
  hparams = transformer_big()
  hparams.max_length = 512
  hparams.shared_source_target_embedding = int(False)
  hparams.learning_rate_warmup_steps = 4000
  hparams.layer_prepostprocess_dropout = 0.1
  hparams.batch_size = 2048
  hparams.learning_rate = 0.05
  return hparams


@registry.register_hparams
def transformer_parsing_ice():
  """Hparams for parsing and tagging Icelandic text."""
  hparams = transformer_base_single_gpu()
  hparams.batch_size = 4096
  hparams.shared_embedding_and_softmax_weights = int(False)
  return hparams


@registry.register_hparams
def transformer_tiny():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 128
  hparams.filter_size = 512
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_small():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 256
  hparams.filter_size = 1024
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_l2():
  hparams = transformer_base()
  hparams.num_hidden_layers = 2
  return hparams


@registry.register_hparams
def transformer_l4():
  hparams = transformer_base()
  hparams.num_hidden_layers = 4
  return hparams


@registry.register_hparams
def transformer_l8():
  hparams = transformer_base()
  hparams.num_hidden_layers = 8
  return hparams


@registry.register_hparams
def transformer_l10():
  hparams = transformer_base()
  hparams.num_hidden_layers = 10
  return hparams


@registry.register_hparams
def transformer_h1():
  hparams = transformer_base()
  hparams.num_heads = 1
  return hparams


@registry.register_hparams
def transformer_h4():
  hparams = transformer_base()
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_h16():
  hparams = transformer_base()
  hparams.num_heads = 16
  return hparams


@registry.register_hparams
def transformer_h32():
  hparams = transformer_base()
  hparams.num_heads = 32
  return hparams


@registry.register_hparams
def transformer_k128():
  hparams = transformer_base()
  hparams.attention_key_channels = 128
  return hparams


@registry.register_hparams
def transformer_k256():
  hparams = transformer_base()
  hparams.attention_key_channels = 256
  return hparams


@registry.register_hparams
def transformer_ff1024():
  hparams = transformer_base()
  hparams.filter_size = 1024
  return hparams


@registry.register_hparams
def transformer_ff4096():
  hparams = transformer_base()
  hparams.filter_size = 4096
  return hparams


@registry.register_hparams
def transformer_dr0():
  hparams = transformer_base()
  hparams.layer_prepostprocess_dropout = 0.0
  return hparams


@registry.register_hparams
def transformer_dr2():
  hparams = transformer_base()
  hparams.layer_prepostprocess_dropout = 0.2
  return hparams


@registry.register_hparams
def transformer_ls0():
  hparams = transformer_base()
  hparams.label_smoothing = 0.0
  return hparams


@registry.register_hparams
def transformer_ls2():
  hparams = transformer_base()
  hparams.label_smoothing = 0.2
  return hparams


@registry.register_hparams
def transformer_hs256():
  hparams = transformer_base()
  hparams.hidden_size = 256
  return hparams


@registry.register_hparams
def transformer_hs1024():
  hparams = transformer_base()
  hparams.hidden_size = 1024
  return hparams


@registry.register_hparams
def transformer_big_dr1():
  hparams = transformer_base()
  hparams.hidden_size = 1024
  hparams.filter_size = 4096
  hparams.num_heads = 16
  hparams.layer_prepostprocess_dropout = 0.1
  return hparams


@registry.register_hparams
def transformer_big_enfr():
  hparams = transformer_big_dr1()
  hparams.shared_embedding_and_softmax_weights = int(False)
  hparams.filter_size = 8192
  hparams.layer_prepostprocess_dropout = 0.1
  return hparams


@registry.register_hparams
def transformer_big_dr2():
  hparams = transformer_big_dr1()
  hparams.layer_prepostprocess_dropout = 0.2
  return hparams


@registry.register_hparams
def transformer_parameter_attention_a():
  hparams = transformer_base()
  hparams.ffn_layer = "parameter_attention"
  hparams.filter_size = 1536
  return hparams


@registry.register_hparams
def transformer_parameter_attention_b():
  hparams = transformer_base()
  hparams.ffn_layer = "parameter_attention"
  hparams.filter_size = 512
  hparams.parameter_attention_key_channels = 1024
  hparams.parameter_attention_value_channels = 1024
  hparams.num_heads = 16
  return hparams


@registry.register_hparams
def transformer_prepend():
  hparams = transformer_base()
  hparams.prepend_mode = "prepend_inputs_masked_attention"
  hparams.max_length = 0
  return hparams


@registry.register_ranged_hparams("transformer_base")
def transformer_base_range(rhp):
  """Small range of hyperparameters."""
  hparams = transformer_base()
  common_hparams.fill_ranged_hparams_from_hparams(hparams, rhp)
  # After starting from base, set intervals for some parameters.
  rhp.set_float("learning_rate", 0.3, 3.0, scale=rhp.LOG_SCALE)
  rhp.set_discrete("learning_rate_warmup_steps",
                   [1000, 2000, 4000, 8000, 16000])
  rhp.set_float("initializer_gain", 0.5, 2.0)
  rhp.set_float("optimizer_adam_beta1", 0.85, 0.95)
  rhp.set_float("optimizer_adam_beta2", 0.97, 0.99)
  rhp.set_float("weight_decay", 0.0, 2.0)


@registry.register_hparams
def transformer_relative():
  """Use relative position embeddings instead of absolute position encodings."""
  hparams = transformer_base()
  hparams.pos = None
  hparams.self_attention_type = "dot_product_relative"
  hparams.max_relative_position = 20
  return hparams


@registry.register_hparams
def transformer_relative_tiny():
  hparams = transformer_relative()
  hparams.num_hidden_layers = 2
  hparams.hidden_size = 128
  hparams.filter_size = 512
  hparams.num_heads = 4
  return hparams


@registry.register_hparams
def transformer_relative_big():
  hparams = transformer_big()
  hparams.pos = None
  hparams.self_attention_type = "dot_product_relative"
  hparams.max_relative_position = 20
  return hparams
