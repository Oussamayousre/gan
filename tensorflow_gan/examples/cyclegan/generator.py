# coding=utf-8
# Copyright 2021 The TensorFlow GAN Authors.
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

"""Defines the CycleGAN generator and discriminator networks."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow.compat.v1 as tf
import tensorflow_gan as tfgan


def _instance_norm(x):
  return tfgan.features.instance_norm(
      x,
      center=True,
      scale=True,
      epsilon=0.001)


def cyclegan_upsample(net,
                      num_outputs,
                      strides,
                      method='conv2d_transpose',
                      pad_mode='REFLECT'):
  """Upsamples the given inputs.

  Args:
    net: A Tensor of size [batch_size, height, width, filters].
    num_outputs: The number of output filters.
    strides: A list of 2 scalars or a 1x2 Tensor indicating the scale,
      relative to the inputs, of the output dimensions. For example, if kernel
      size is [2, 3], then the output height and width will be twice and three
      times the input size.
    method: The upsampling method: 'nn_upsample_conv', 'bilinear_upsample_conv',
      or 'conv2d_transpose'.
    pad_mode: mode for tf.pad, one of "CONSTANT", "REFLECT", or "SYMMETRIC".

  Returns:
    A Tensor which was upsampled using the specified method.

  Raises:
    ValueError: if `method` is not recognized.
  """
  with tf.variable_scope('upconv'):
    net_shape = tf.shape(input=net)
    height = net_shape[1]
    width = net_shape[2]

    # Reflection pad by 1 in spatial dimensions (axes 1, 2 = h, w) to make a 3x3
    # 'valid' convolution produce an output with the same dimension as the
    # input.
    spatial_pad_1 = np.array([[0, 0], [1, 1], [1, 1], [0, 0]])

    if method == 'nn_upsample_conv':
      net = tf.image.resize(
          net, [strides[0] * height, strides[1] * width],
          method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
      net = tf.pad(tensor=net, paddings=spatial_pad_1, mode=pad_mode)
      net = _conv2d(net, num_outputs, 3)
      net = _instance_norm(net)
      net = tf.nn.relu(net)
    elif method == 'bilinear_upsample_conv':
      net = tf.image.resize(
          net, [strides[0] * height, strides[1] * width],
          method=tf.image.ResizeMethod.BILINEAR)
      net = tf.pad(tensor=net, paddings=spatial_pad_1, mode=pad_mode)
      net = _conv2d(net, num_outputs, 3)
      net = _instance_norm(net)
      net = tf.nn.relu(net)
    elif method == 'conv2d_transpose':
      # This corrects 1 pixel offset for images with even width and height.
      # conv2d is left aligned and conv2d_transpose is right aligned for even
      # sized images (while doing 'SAME' padding).
      # Note: This doesn't reflect actual model in paper.
      net = tf.layers.conv2d_transpose(
          net,
          num_outputs,
          kernel_size=[3, 3],
          strides=strides,
          padding='valid')
      net = tf.nn.relu(net)
      net = net[:, 1:, 1:, :]
    else:
      raise ValueError('Unknown method: [%s]' % method)

    return net


def _dynamic_or_static_shape(tensor):
  static_shape = tensor.shape
  shape = tf.shape(input=tensor)
  return static_shape.as_list() if static_shape.is_fully_defined() else shape


def _conv2d(net, num_filters, kernel_size, strides=1):
  return tf.layers.conv2d(
      net,
      num_filters,
      kernel_size,
      strides,
      padding='VALID',
      kernel_initializer=tf.random_normal_initializer(0, 0.02),
      use_bias=False)


def cyclegan_generator_resnet(images,
                              num_resnet_blocks=6,
                              num_filters=64,
                              upsample_fn=cyclegan_upsample,
                              kernel_size=3,
                              tanh_linear_slope=0.0):
  """Defines the cyclegan resnet network architecture.

  As closely as possible following
  https://github.com/junyanz/CycleGAN/blob/master/models/architectures.lua#L232

  FYI: This network requires input height and width to be divisible by 4 in
  order to generate an output with shape equal to input shape. Assertions will
  catch this if input dimensions are known at graph construction time, but
  there's no protection if unknown at graph construction time (you'll see an
  error).

  Args:
    images: Input image tensor of shape [batch_size, h, w, 3].
    num_resnet_blocks: Number of ResNet blocks in the middle of the generator.
    num_filters: Number of filters of the first hidden layer.
    upsample_fn: Upsampling function for the decoder part of the generator.
    kernel_size: Size w or list/tuple [h, w] of the filter kernels for all inner
      layers.
    tanh_linear_slope: Slope of the linear function to add to the tanh over the
      logits.

  Returns:
    A `Tensor` representing the model output and a dictionary of model end
      points.

  Raises:
    ValueError: If the input height or width is known at graph construction time
      and not a multiple of 4.
  """
  end_points = {}

  input_size = images.shape.as_list()
  height, width = input_size[1], input_size[2]
  if height and height % 4 != 0:
    raise ValueError('The input height must be a multiple of 4.')
  if width and width % 4 != 0:
    raise ValueError('The input width must be a multiple of 4.')
  num_outputs = input_size[3]

  if not isinstance(kernel_size, (list, tuple)):
    kernel_size = [kernel_size, kernel_size]

  kernel_height = kernel_size[0]
  kernel_width = kernel_size[1]
  pad_top = (kernel_height - 1) // 2
  pad_bottom = kernel_height // 2
  pad_left = (kernel_width - 1) // 2
  pad_right = kernel_width // 2
  paddings = np.array(
      [[0, 0], [pad_top, pad_bottom], [pad_left, pad_right], [0, 0]],
      dtype=np.int32)
  spatial_pad_3 = np.array([[0, 0], [3, 3], [3, 3], [0, 0]])

  ###########
  # Encoder #
  ###########
  with tf.variable_scope('input'):
    # 7x7 input stage
    net = tf.pad(tensor=images, paddings=spatial_pad_3, mode='REFLECT')
    net = _conv2d(net, num_filters, kernel_size=7)
    net = _instance_norm(net)
    net = tf.nn.relu(net)
    end_points['encoder_0'] = net

  with tf.variable_scope('encoder'):
    net = tf.pad(tensor=net, paddings=paddings, mode='REFLECT')
    net = _conv2d(net, num_filters * 2, kernel_size, strides=2)
    net = _instance_norm(net)
    net = tf.nn.relu(net)
    end_points['encoder_1'] = net
    net = tf.pad(tensor=net, paddings=paddings, mode='REFLECT')
    net = _conv2d(net, num_filters * 4, kernel_size, strides=2)
    net = _instance_norm(net)
    net = tf.nn.relu(net)
    end_points['encoder_2'] = net

    ###################
    # Residual Blocks #
    ###################
    with tf.variable_scope('residual_blocks'):
      for block_id in xrange(num_resnet_blocks):
        with tf.variable_scope('block_{}'.format(block_id)):
          res_net = tf.pad(tensor=net, paddings=paddings, mode='REFLECT')
          res_net = _conv2d(res_net, num_filters * 4, kernel_size)
          res_net = _instance_norm(res_net)
          res_net = tf.nn.relu(res_net)
          res_net = tf.pad(tensor=res_net, paddings=paddings, mode='REFLECT')
          res_net = _conv2d(res_net, num_filters * 4, kernel_size)
          res_net = _instance_norm(res_net)
          net += res_net
          end_points['resnet_block_%d' % block_id] = net

    ###########
    # Decoder #
    ###########
    with tf.variable_scope('decoder'):
      with tf.variable_scope('decoder1'):
        net = upsample_fn(net, num_outputs=num_filters * 2, strides=[2, 2])
      end_points['decoder1'] = net

      with tf.variable_scope('decoder2'):
        net = upsample_fn(net, num_outputs=num_filters, strides=[2, 2])
      end_points['decoder2'] = net

    with tf.variable_scope('output'):
      net = tf.pad(tensor=net, paddings=spatial_pad_3, mode='REFLECT')
      logits = _conv2d(net, num_outputs, 7)
      logits = tf.reshape(logits, _dynamic_or_static_shape(images))

      end_points['logits'] = logits
      end_points['predictions'] = tf.tanh(logits) + logits * tanh_linear_slope

  return end_points['predictions'], end_points
