# Lint as: python3
# Copyright 2020 The TensorFlow Probability Authors.
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
"""Lorenz System model."""

import functools
import numpy as np

import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.internal import dtype_util
from tensorflow_probability.python.internal import tensor_util

from inference_gym.internal import data
from inference_gym.targets import bayesian_model
from inference_gym.targets import model
from inference_gym.targets.ground_truth import convection_lorenz_bridge
from inference_gym.targets.ground_truth import convection_lorenz_bridge_unknown_scales

tfb = tfp.bijectors
tfd = tfp.distributions

__all__ = [
    'LorenzSystem',
    'LorenzSystemUnknownScales',
    'ConvectionLorenzBridge',
    'ConvectionLorenzBridgeUnknownScales',
]

Root = tfd.JointDistributionCoroutine.Root


def lorenz_system_prior_fn(num_timesteps, innovation_scale, step_size,
                           dtype=tf.float32):
  """Generative process for the Lorenz System model."""
  innovation_scale = tensor_util.convert_nonref_to_tensor(
      innovation_scale, name='innovation_scale', dtype=dtype)
  step_size = tensor_util.convert_nonref_to_tensor(
      step_size, name='step_size', dtype=dtype)
  loc = yield Root(tfd.Sample(tfd.Normal(0., 1.), sample_shape=3))
  for _ in range(num_timesteps - 1):
    x, y, z = tf.unstack(loc, axis=-1)
    dx = 10 * (y - x)
    dy = x * (28 - z) - y
    dz = x * y - 8 / 3 * z
    delta = tf.stack([dx, dy, dz], axis=-1)
    loc = yield tfd.Independent(
        tfd.Normal(loc + step_size * delta,
                   tf.sqrt(step_size) * innovation_scale[..., tf.newaxis]),
        reinterpreted_batch_ndims=1)


def lorenz_system_unknown_scales_prior_fn(num_timesteps,
                                          step_size,
                                          dtype=tf.float32):
  innovation_scale = yield Root(tfd.LogNormal(0., 2., name='innovation_scale'))
  _ = yield Root(tfd.LogNormal(0., 2., name='observation_scale'))
  yield from lorenz_system_prior_fn(num_timesteps=num_timesteps,
                                    innovation_scale=innovation_scale,
                                    step_size=step_size,
                                    dtype=dtype)


def lorenz_system_log_likelihood_fn(params, observed_values, observation_scale,
                                    observation_index, observation_mask,
                                    dtype=tf.float32):
  """Likelihood of a series under the Lorenz System model."""
  if observation_scale is None:  # Scales are random variables.
    (_, observation_scale), params = params[:2], params[2:]

  observed_values = tensor_util.convert_nonref_to_tensor(
      observed_values, name='observation_values', dtype=dtype)
  observation_scale = tensor_util.convert_nonref_to_tensor(
      observation_scale, name='observation_scale', dtype=dtype)
  num_observations = tf.compat.dimension_value(observed_values.shape[0])
  observation_indices = list(np.arange(num_observations)[observation_mask])
  series_values = tf.stack(params, axis=-2)[..., observation_index]
  masked_series = tf.gather(series_values, observation_indices, axis=-1)
  masked_observations = tf.gather(observed_values, observation_indices, axis=-1)

  return tfd.Independent(
      tfd.Normal(masked_series, observation_scale[..., tf.newaxis]),
      reinterpreted_batch_ndims=1).log_prob(masked_observations)


class LorenzSystem(bayesian_model.BayesianModel):
  """Construct a Lorenz System model.

  This class models the Lorenz System, a three-dimensional nonlinear dynamical
  system used to model atmospheric convection. This model defines a stochastic
  variant that follows the following differential equation:
  ```none
  x(0) ~ Normal(0, 1)
  y(0) ~ Normal(0, 1)
  z(0) ~ Normal(0, 1)

  x'(t) = 10 * (y(t) - x(t)) + w_x(t)
  y'(t) = x(t) * (28 - z(t)) - y(t) + w_y(t)
  z'(t) = x(t) * y(t) - 8 / 3 * z(t) + w_z(t)
  ```
  where `w_x(t)`, `w_y(t)` and `w_z(t)` are Gaussian innovation noise processes
  with a provided scale `innovation_scale`.

  The differential equation is numerically integrated using the Euler-Mariyama
  method whereafter the three time series `x`, `y`, and `z` (or a subset of
  them) are observed with Gaussian observation noise. Observations may be
  occluded with an `observation_mask` parameter.
  """

  def __init__(self,
               observed_values,
               innovation_scale,
               observation_scale,
               observation_mask,
               observation_index,
               step_size,
               name='lorenz_system',
               pretty_name='Lorenz System'):
    """Constructs a Lorenz System model.

    Args:
      observed_values: A `float` array of observed values whose first dimension
        corresponds to number of integration steps.
      innovation_scale: Python `float`, for the scale of the noise process in
        the Lorenz system dynamics.
      observation_scale: Python `float`, for the scale of the observation noise.
      observation_mask: Array of `bool` that occludes observed values.
      observation_index: Python `int` or `list` of `int`s that determines which
        latent time series are observed.
      step_size: Python `float` used for integrating the Lorenz dynamics.
      name: Python `str` name prefixed to Ops created by this class.
      pretty_name: A Python `str`. The pretty name of this model.
    """
    with tf.name_scope(name):
      dtype = dtype_util.common_dtype(
          [observed_values, innovation_scale, observation_scale, step_size],
          dtype_hint=tf.float32)

      if not isinstance(observation_index, int):
        raise NotImplementedError('Observing multiple time series is not yet'
                                  ' supported.')

      num_timesteps = observed_values.shape[0]

      self._prior_dist = tfd.JointDistributionCoroutine(
          functools.partial(
              lorenz_system_prior_fn,
              num_timesteps=num_timesteps,
              innovation_scale=innovation_scale,
              step_size=step_size,
              dtype=dtype))

      self._log_likelihood_fn = functools.partial(
          lorenz_system_log_likelihood_fn,
          observed_values=observed_values,
          observation_scale=observation_scale,
          observation_mask=observation_mask,
          observation_index=observation_index,
          dtype=dtype)

      def _ext_identity(params):
        return tf.stack(params, -2)

      sample_transformations = {
          'identity':
              model.Model.SampleTransformation(
                  fn=_ext_identity,
                  pretty_name='Identity',
              )
      }

      event_space_bijector = type(
          self._prior_dist.dtype)(*([tfb.Identity()] * num_timesteps))
    super(LorenzSystem, self).__init__(
        default_event_space_bijector=event_space_bijector,
        event_shape=self._prior_dist.event_shape,
        dtype=self._prior_dist.dtype,
        name=name,
        pretty_name=pretty_name,
        sample_transformations=sample_transformations,
    )

  def _prior_distribution(self):
    return self._prior_dist

  def log_likelihood(self, params):
    return self._log_likelihood_fn(params)


class ConvectionLorenzBridge(LorenzSystem):
  """A partially observed `LorenzSystem` with missing middle observations.

  A `ConvectionLorenzBridge` is a Lorenz system where only the convection
  (`x(t)`) has been observed and the middle ten observations are missing. It is
  based on the Lorenze bridge baseline used in [1].

  #### References

  1. Ambrogioni, Luca, Max Hinne, and Marcel van Gerven. "Automatic structured
     variational inference." arXiv preprint arXiv:2002.00643 (2020).
  """

  GROUND_TRUTH_MODULE = convection_lorenz_bridge

  def __init__(self,
               name='convection_lorenz_bridge',
               pretty_name='Ambrogioni Lorenz System'):
    dataset = data.convection_lorenz_bridge()
    params = dict(dataset, name=name, pretty_name=pretty_name)
    super(ConvectionLorenzBridge, self).__init__(**params)


class LorenzSystemUnknownScales(bayesian_model.BayesianModel):
  """Construct a Lorenz System model with unknown scale parameters.

  This class models a Lorenz System, a three-dimensional nonlinear dynamical
  system used to model atmospheric convection. The differential equation model
  is identical to that of `LorenzSystem`, which assumes fixed parameters, but
  here the scale parameters of the innovation and observation distributions are
  additionally modeled as unknown random variables:

  ```
  innovation_scale ~ LogNormal(0., 2.)
  observation_scale ~ LogNormal(0., 2.)

  # IID noise processes.
  w_x(t) ~ Normal(0., innovation_scale)
  w_y(t) ~ Normal(0., innovation_scale)
  w_z(t) ~ Normal(0., innovation_scale)

  x(0) ~ Normal(0, 1)
  y(0) ~ Normal(0, 1)
  z(0) ~ Normal(0, 1)

  x'(t) = 10 * (y(t) - x(t)) + w_x(t)
  y'(t) = x(t) * (28 - z(t)) - y(t) + w_y(t)
  z'(t) = x(t) * y(t) - 8 / 3 * z(t) + w_z(t)

  # Noisy observations.
  obs_x(t) ~ Normal(x(t), observation_scale)
  obs_y(t) ~ Normal(y(t), observation_scale)
  obs_z(t) ~ Normal(z(t), observation_scale)
  ```
  """

  def __init__(self,
               observed_values,
               observation_mask,
               observation_index,
               step_size,
               name='lorenz_system',
               pretty_name='Lorenz System'):
    """Constructs a Lorenz System model.

    Args:
      observed_values: A `float` array of observed values whose first dimension
        corresponds to number of integration steps.
      observation_mask: Array of `bool` that occludes observed values.
      observation_index: Python `int` or `list` of `int`s that determines which
        latent time series are observed.
      step_size: Python `float` used for integrating the Lorenz dynamics.
      name: Python `str` name prefixed to Ops created by this class.
      pretty_name: A Python `str`. The pretty name of this model.
    """
    with tf.name_scope(name):
      dtype = dtype_util.common_dtype(
          [observed_values, step_size],
          dtype_hint=tf.float32)

      if not isinstance(observation_index, int):
        raise NotImplementedError('Observing multiple time series is not yet'
                                  ' supported.')

      num_timesteps = observed_values.shape[0]

      self._prior_dist = tfd.JointDistributionCoroutine(
          functools.partial(
              lorenz_system_unknown_scales_prior_fn,
              num_timesteps=num_timesteps,
              step_size=step_size,
              dtype=dtype))

      self._log_likelihood_fn = functools.partial(
          lorenz_system_log_likelihood_fn,
          observed_values=observed_values,
          observation_scale=None,
          observation_mask=observation_mask,
          observation_index=observation_index,
          dtype=dtype)

      def _ext_identity(params):
        return {'innovation_scale': params[0],
                'observation_scale': params[1],
                'latents': tf.stack(params[2:], -2)}

      sample_transformations = {
          'identity':
              model.Model.SampleTransformation(
                  fn=_ext_identity,
                  pretty_name='Identity',
                  dtype={'innovation_scale': tf.float32,
                         'observation_scale': tf.float32,
                         'latents': tf.float32}
              )
      }

      event_space_bijector = type(
          self._prior_dist.dtype)(*([tfb.Softplus(),
                                     tfb.Softplus()
                                     ] + [tfb.Identity()] * num_timesteps))
    super(LorenzSystemUnknownScales, self).__init__(
        default_event_space_bijector=event_space_bijector,
        event_shape=self._prior_dist.event_shape,
        dtype=self._prior_dist.dtype,
        name=name,
        pretty_name=pretty_name,
        sample_transformations=sample_transformations,
    )

  def _prior_distribution(self):
    return self._prior_dist

  def log_likelihood(self, params):
    return self._log_likelihood_fn(params)


class ConvectionLorenzBridgeUnknownScales(LorenzSystemUnknownScales):
  """A partially observed `LorenzSystem` with missing middle observations.

  A `ConvectionLorenzBridge` is a Lorenz system where only the convection
  (`x(t)`) has been observed and the middle ten observations are missing. It is
  based on the Lorenze bridge baseline used in [1]. In this version, the
  scale parameters of the innovation and observation noise processes are
  treated as unknown variables to be inferred.

  #### References

  1. Ambrogioni, Luca, Max Hinne, and Marcel van Gerven. "Automatic structured
     variational inference." arXiv preprint arXiv:2002.00643 (2020).
  """

  GROUND_TRUTH_MODULE = convection_lorenz_bridge_unknown_scales

  def __init__(self,
               name='convection_lorenz_bridge_unknown_scales',
               pretty_name='Ambrogioni Lorenz System'):
    dataset = data.convection_lorenz_bridge()
    del dataset['innovation_scale']
    del dataset['observation_scale']
    params = dict(dataset, name=name, pretty_name=pretty_name)
    super(ConvectionLorenzBridgeUnknownScales, self).__init__(**params)
