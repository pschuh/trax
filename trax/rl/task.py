# coding=utf-8
# Copyright 2021 The Trax Authors.
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

# Lint as: python3
"""Classes for defining RL tasks in Trax."""

import collections
import os

import gin
import gym
import numpy as np

from trax import fastmath
from trax.supervised import training



class _TimeStep:
  """A single step of interaction with a RL environment.

  TimeStep stores a single step in the trajectory of an RL run:
  * observation (same as observation) at the beginning of the step
  * action that was takes (or None if none taken yet)
  * reward gotten when the action was taken (or None if action wasn't taken)
  * log-probability of the action taken (or None if not specified)
  * discounted return from that state (includes the reward from this step)
  """

  def __init__(
      self, observation, action=None, reward=None, dist_inputs=None, done=None,
      mask=None
  ):
    self.observation = observation
    self.action = action
    self.reward = reward
    self.dist_inputs = dist_inputs
    self.done = done
    self.mask = mask
    self.discounted_return = 0.0


# Tuple for representing trajectories and batches of them in numpy; immutable.
TrajectoryNp = collections.namedtuple('TrajectoryNp', [
    'observations',
    'actions',
    'dist_inputs',
    'rewards',
    'returns',
    'dones',
    'mask',
])


# Same as TrajectoryNp, for timesteps. This is separate for documentation
# purposes, but it's functionally redundant.
# TODO(pkozakowski): Consider merging with TrajectoryNp and finding a common
# name. At the very least it should be merged with _TimeStep - I'm not doing it
# for now to keep backward compatibility with batch RL experiments.
TimeStepNp = collections.namedtuple('TimeStepNp', [
    'observation',
    'action',
    'dist_inputs',
    'reward',
    'return_',
    'done',
    'mask',
])


class Trajectory:
  """A trajectory of interactions with a RL environment.

  Trajectories are created when interacting with an RL environment. They can
  be prolonged and sliced and when completed, allow to re-calculate returns.
  """

  def __init__(self, observation):
    # TODO(lukaszkaiser): add support for saving and loading trajectories,
    # reuse code from base_trainer.dump_trajectories and related functions.
    if observation is not None:
      self._timesteps = [_TimeStep(observation)]
    self._trajectory_np = None
    self._cached_to_np_args = None

  def __len__(self):
    return len(self._timesteps)

  def __str__(self):
    return str([(ts.observation, ts.action, ts.reward, ts.done)
                for ts in self._timesteps])

  def __repr__(self):
    return repr([(ts.observation, ts.action, ts.reward, ts.done)
                 for ts in self._timesteps])

  def __getitem__(self, key):
    t = Trajectory(None)
    t._timesteps = self._timesteps[key]  # pylint: disable=protected-access
    return t

  @property
  def timesteps(self):
    return self._timesteps

  @property
  def total_return(self):
    """Sum of all rewards in this trajectory."""
    return sum([t.reward or 0.0 for t in self._timesteps])

  @property
  def last_observation(self):
    """Return the last observation in this trajectory."""
    last_timestep = self._timesteps[-1]
    return last_timestep.observation

  @property
  def done(self):
    """Returns whether the trajectory is finished."""
    if len(self._timesteps) < 2:
      return False
    second_last_timestep = self._timesteps[-2]
    return second_last_timestep.done

  @done.setter
  def done(self, done):
    """Sets the `done` flag in the last timestep."""
    if len(self._timesteps) < 2:
      raise ValueError('No interactions yet in the trajectory.')
    last_timestep = self._timesteps[-2]
    last_timestep.done = done

  def extend(self, action, dist_inputs, new_observation, reward, done, mask=1):
    """Take action in the last state, getting reward and going to new state."""
    last_timestep = self._timesteps[-1]
    last_timestep.action = action
    last_timestep.dist_inputs = dist_inputs
    last_timestep.reward = reward
    last_timestep.done = done
    last_timestep.mask = mask
    new_timestep = _TimeStep(new_observation)
    self._timesteps.append(new_timestep)

  def calculate_returns(self, gamma):
    """Calculate discounted returns."""
    ret = 0.0
    for timestep in reversed(self._timesteps):
      cur_reward = timestep.reward or 0.0
      ret = gamma * ret + cur_reward
      timestep.discounted_return = ret

  def _default_timestep_to_np(self, ts):
    """Default way to convert timestep to numpy."""
    return fastmath.nested_map(np.array, TimeStepNp(
        observation=ts.observation,
        action=ts.action,
        dist_inputs=ts.dist_inputs,
        reward=ts.reward,
        done=ts.done,
        return_=ts.discounted_return,
        mask=ts.mask,
    ))

  def to_np(self, margin=0, timestep_to_np=None):
    """Create a tuple of numpy arrays from a given trajectory."""
    timestep_to_np = timestep_to_np or self._default_timestep_to_np
    args = (margin, timestep_to_np)

    # Return the cached result if the arguments agree and the trajectory has not
    # grown.
    if self._trajectory_np:
      result_length = len(self) + margin - 1
      length_ok = self._trajectory_np.observations.shape[0] == result_length
      if args == self._cached_to_np_args and length_ok:
        return self._trajectory_np

    observations, actions, dist_inputs, rewards, returns, dones, masks = (
        [], [], [], [], [], [], []
    )
    for timestep in self._timesteps:
      if timestep.action is None:
        obs = timestep_to_np(timestep).observation
        observations.append(obs)
      else:
        timestep_np = timestep_to_np(timestep)
        observations.append(timestep_np.observation)
        actions.append(timestep_np.action)
        dist_inputs.append(timestep_np.dist_inputs)
        rewards.append(timestep_np.reward)
        dones.append(timestep_np.done)
        returns.append(timestep_np.return_)
        masks.append(timestep_np.mask)

    # TODO(pkozakowski): The case len(obs) == 1 is for handling
    # "dummy trajectories", that are only there to determine data shapes. Check
    # if they're still required.
    if len(observations) > 1:
      # Extend the trajectory with a given margin - this is to make sure that
      # the networks always "see" the "done" states in the training data, even
      # when a suffix is added to the trajectory slice for better estimation of
      # returns.
      # We set `mask` to 0, so the added timesteps don't influence the loss. We
      # set `done` to True for easier implementation of advantage estimators.
      # The rest of the fields don't matter, so we set them to 0 for easy
      # debugging (unless they're None). The list of observations is longer, so
      # we pad it with margin - 1.
      masks.extend([0] * margin)
      dones.extend([True] * margin)
      observations.extend([np.zeros_like(observations[-1])] * (margin - 1))
      for x in (actions, dist_inputs, rewards, returns):
        filler = None if x[-1] is None else np.zeros_like(x[-1])
        x.extend([filler] * margin)

    def stack(x):
      if not x:
        return None
      return fastmath.nested_stack(x)

    trajectory_np = TrajectoryNp(**{  # pylint: disable=g-complex-comprehension
        key: stack(value) for (key, value) in [
            ('observations', observations),
            ('actions', actions),
            ('dist_inputs', dist_inputs),
            ('rewards', rewards),
            ('dones', dones),
            ('returns', returns),
            ('mask', masks),
        ]
    })

    self._trajectory_np = trajectory_np
    self._cached_to_np_args = args

    return trajectory_np


def play(env, policy, dm_suite=False, max_steps=None, last_observation=None):
  """Play an episode in env taking actions according to the given policy.

  Environment is first reset and an from then on, a game proceeds. At each
  step, the policy is asked to choose an action and the environment moves
  forward. A Trajectory is created in that way and returns when the episode
  finished, which is either when env returns `done` or max_steps is reached.

  Args:
    env: the environment to play in, conforming to gym.Env or
      DeepMind suite interfaces.
    policy: a function taking a Trajectory and returning a pair consisting
      of an action (int or float) and the confidence in that action (float,
      defined as the log of the probability of taking that action).
    dm_suite: whether we are using the DeepMind suite or the gym interface
    max_steps: for how many steps to play.
    last_observation: last observation from a previous trajectory slice, used to
      begin a new one. Controls whether we reset the environment at the
      beginning - if `None`, resets the env and starts the slice from the
      observation got from reset().

  Returns:
    a completed trajectory slice that was just played.
  """
  done = False
  cur_step = 0
  if last_observation is None:
    # TODO(pkozakowski): Make a Gym wrapper over DM envs to get rid of branches
    # like that.
    last_observation = env.reset().observation if dm_suite else env.reset()
  cur_trajectory = Trajectory(last_observation)
  while not done and (max_steps is None or cur_step < max_steps):
    action, dist_inputs = policy(cur_trajectory)
    step = env.step(action)
    if dm_suite:
      observation_reward_done = (
          step.observation, step.reward, step.step_type.last()
      )
    else:
      observation_reward_done = step[:3]
    cur_trajectory.extend(action, dist_inputs, *observation_reward_done)
    cur_step += 1
    (_, _, done) = observation_reward_done
  return cur_trajectory


def _zero_pad(x, pad, axis):
  """Helper for np.pad with 0s for single-axis case."""
  pad_widths = [(0, 0)] * len(x.shape)
  pad_widths[axis] = pad  # Padding on axis.
  return np.pad(x, pad_widths, mode='constant',
                constant_values=x.dtype.type(0))


def _random_policy(action_space):
  return lambda _: (action_space.sample(), None)


def _sample_proportionally(inputs, weights):
  """Sample an element from the inputs list proportionally to weights.

  Args:
    inputs: a list, we will return one element of this list.
    weights: a list of numbers of the same length as inputs; we will sample
      the k-th input with probability weights[k] / sum(weights).

  Returns:
    an element from inputs.
  """
  l = len(inputs)
  if l != len(weights):
    raise ValueError(f'Inputs and weights must have the same length, but do not'
                     f': {l} != {len(weights)}')
  weights_sum = float(sum(weights))
  norm_weights = [w / weights_sum for w in weights]
  idx = np.random.choice(l, p=norm_weights)
  return inputs[int(idx)]


@gin.configurable
class RLTask:
  """A RL task: environment and a collection of trajectories."""

  def __init__(self, env=gin.REQUIRED,
               initial_trajectories=1,
               gamma=0.99,
               dm_suite=False,
               random_starts=True,
               max_steps=None,
               time_limit=None,
               timestep_to_np=None,
               num_stacked_frames=1,
               n_replay_epochs=1):
    r"""Configures a RL task.

    Args:
      env: Environment confirming to the gym.Env interface or a string,
        in which case `gym.make` will be called on this string to create an env.
      initial_trajectories: either a dict or list of Trajectories to use
        at start or an int, in which case that many trajectories are
        collected using a random policy to play in env. It can be also a string
        and then it should direct to the location where previously recorded
        trajectories are stored.
      gamma: float: discount factor for calculating returns.
      dm_suite: whether we are using the DeepMind suite or the gym interface
      random_starts: use random starts for training of Atari agents.
      max_steps: optional int: cut all trajectory slices at that many steps.
        The trajectory will be continued in the next epochs, up to `time_limit`.
      time_limit: optional int: stop all trajectories after that many steps (or
        after getting "done"). If `None`, use the same value as `max_steps`.
      timestep_to_np: a function that turns a timestep into a numpy array
        (ie., a tensor); if None, we just use the state of the timestep to
        represent it, but other representations (such as embeddings that include
        actions or serialized representations) can be passed here.
      num_stacked_frames: the number of stacked frames for Atari.
      n_replay_epochs: the size of the replay buffer expressed in epochs.
    """
    if isinstance(env, str):
      self._env_name = env
      if dm_suite:
        eval_env = None
        env = None
      else:
        env = gym.make(self._env_name)
        eval_env = gym.make(self._env_name)
    else:
      self._env_name = type(env).__name__
      eval_env = env
    self._env = env
    self._eval_env = eval_env
    self._dm_suite = dm_suite
    self._max_steps = max_steps
    if time_limit is None:
      time_limit = max_steps
    self._time_limit = time_limit
    self._gamma = gamma
    self._initial_trajectories = initial_trajectories
    self._last_observation = None
    self._n_steps_left = time_limit
    # TODO(lukaszkaiser): find a better way to pass initial trajectories,
    # whether they are an explicit list, a file, or a number of random ones.
    if isinstance(initial_trajectories, int):
      if initial_trajectories > 0:
        initial_trajectories = [
            self.play(_random_policy(self.action_space))
            for _ in range(initial_trajectories)
        ]
      else:
        initial_trajectories = [
            # Whatever we gather here is intended to be removed
            # in PolicyTrainer. Here we just gather some example inputs.
            self.play(_random_policy(self.action_space))
        ]
    if isinstance(initial_trajectories, str):
      initial_trajectories = self.load_initial_trajectories_from_path(
          initial_trajectories_path=initial_trajectories)
    if isinstance(initial_trajectories, list):
      initial_trajectories = {0: initial_trajectories}
    self._timestep_to_np = timestep_to_np
    # Stored trajectories are indexed by epoch and within each epoch they
    # are stored in the order of generation so we can implement replay buffers.
    # TODO(lukaszkaiser): use dump_trajectories from BaseTrainer to allow
    # saving and reading trajectories from disk.
    self._trajectories = collections.defaultdict(list)
    self._trajectories.update(initial_trajectories)
    # When we repeatedly save, trajectories for many epochs do not change, so
    # we don't need to save them again. This keeps track which are unchanged.
    self._saved_epochs_unchanged = []
    self._n_replay_epochs = n_replay_epochs
    self._n_trajectories = 0
    self._n_interactions = 0

  @property
  def env(self):
    return self._env

  @property
  def env_name(self):
    return self._env_name

  @property
  def max_steps(self):
    return self._max_steps

  @property
  def gamma(self):
    return self._gamma

  @property
  def action_space(self):
    if self._dm_suite:
      return gym.spaces.Discrete(self._env.action_spec().num_values)
    else:
      return self._env.action_space

  @property
  def observation_space(self):
    """Returns the env's observation space in a Gym interface."""
    if self._dm_suite:
      return gym.spaces.Box(
          shape=self._env.observation_spec().shape,
          dtype=self._env.observation_spec().dtype,
          low=float('-inf'),
          high=float('+inf'),
      )
    else:
      return self._env.observation_space

  @property
  def trajectories(self):
    return self._trajectories

  @property
  def timestep_to_np(self):
    return self._timestep_to_np

  @timestep_to_np.setter
  def timestep_to_np(self, ts):
    self._timestep_to_np = ts

  def _epoch_filename(self, base_filename, epoch):
    """Helper function: file name for saving the given epoch."""
    # If base is /foo/task.pkl, we save epoch 1 under /foo/task_epoch1.pkl.
    filename, ext = os.path.splitext(base_filename)
    return filename + '_epoch' + str(epoch) + ext

  def set_n_replay_epochs(self, n_replay_epochs):
    self._n_replay_epochs = n_replay_epochs

  def load_initial_trajectories_from_path(self,
                                          initial_trajectories_path,
                                          dictionary_file='trajectories.pkl',
                                          start_epoch_to_load=0):
    """Initialize trajectories task from file."""
    # We assume that this is a dump generated by Trax
    dictionary_file = os.path.join(initial_trajectories_path, dictionary_file)
    dictionary = training.unpickle_from_file(dictionary_file, gzip=False)
    # TODO(henrykm): as currently implemented this accesses only
    # at most the last n_replay_epochs - this should be more flexible
    epochs_to_load = dictionary['all_epochs'][start_epoch_to_load:]

    all_trajectories = []
    for epoch in epochs_to_load:
      trajectories = training.unpickle_from_file(
          self._epoch_filename(dictionary_file, epoch), gzip=True)
      all_trajectories += trajectories
    return all_trajectories

  def init_from_file(self, file_name):
    """Initialize this task from file."""
    dictionary = training.unpickle_from_file(file_name, gzip=False)
    self._n_trajectories = dictionary['n_trajectories']
    self._n_interactions = dictionary['n_interactions']
    self._max_steps = dictionary['max_steps']
    self._gamma = dictionary['gamma']
    epochs_to_load = dictionary['all_epochs'][-self._n_replay_epochs:]

    for epoch in epochs_to_load:
      trajectories = training.unpickle_from_file(
          self._epoch_filename(file_name, epoch), gzip=True)
      self._trajectories[epoch] = trajectories
    self._saved_epochs_unchanged = epochs_to_load

  def save_to_file(self, file_name):
    """Save this task to file."""
    # Save trajectories from new epochs first.
    epochs_to_save = [e for e in self._trajectories
                      if e not in self._saved_epochs_unchanged]
    for epoch in epochs_to_save:
      training.pickle_to_file(self._trajectories[epoch],
                              self._epoch_filename(file_name, epoch),
                              gzip=True)
    # Now save the list of epochs (so the trajectories are already there,
    # even in case of preemption).
    dictionary = {'n_interactions': self._n_interactions,
                  'n_trajectories': self._n_trajectories,
                  'max_steps': self._max_steps,
                  'gamma': self._gamma,
                  'all_epochs': list(self._trajectories.keys())}
    training.pickle_to_file(dictionary, file_name, gzip=False)

  def play(self, policy, max_steps=None, only_eval=False):
    """Play an episode in env taking actions according to the given policy."""
    if max_steps is None:
      max_steps = self._max_steps
    if only_eval:
      cur_trajectory = play(
          self._eval_env, policy, self._dm_suite,
          # Only step up to the time limit.
          max_steps=min(max_steps, self._time_limit),
          # Always reset at the beginning of an eval episode.
          last_observation=None,
      )
    else:
      cur_trajectory = play(
          self._env, policy, self._dm_suite,
          # Only step up to the time limit, used up by all slices of the same
          # trajectory.
          max_steps=min(max_steps, self._n_steps_left),
          # Pass the environmnent state between play() calls, so one episode can
          # span multiple training epochs.
          # NOTE: Cutting episodes between epochs may hurt e.g. with
          # Transformers.
          # TODO(pkozakowski): Join slices together if this becomes a problem.
          last_observation=self._last_observation,
      )
      # Update the number of steps left to reach the time limit.
      # NOTE: This should really be done as an env wrapper.
      # TODO(pkozakowski): Do that once we wrap the DM envs in Gym interface.
      # The initial reset doesn't count.
      self._n_steps_left -= len(cur_trajectory) - 1
      assert self._n_steps_left >= 0
      if self._n_steps_left == 0:
        cur_trajectory.done = True
    # Pass the last observation between trajectory slices.
    if cur_trajectory.done:
      self._last_observation = None
      if not only_eval:
        # Reset the time limit.
        self._n_steps_left = self._time_limit
    else:
      self._last_observation = cur_trajectory.last_observation
    cur_trajectory.calculate_returns(self._gamma)
    return cur_trajectory

  def collect_trajectories(
      self, policy,
      n_trajectories=None,
      n_interactions=None,
      only_eval=False,
      max_steps=None,
      epoch_id=1,
  ):
    """Collect experience in env playing the given policy."""
    max_steps = max_steps or self.max_steps
    if n_trajectories is not None:
      new_trajectories = [self.play(policy, max_steps=max_steps,
                                    only_eval=only_eval)
                          for _ in range(n_trajectories)]
    elif n_interactions is not None:
      new_trajectories = []
      while n_interactions > 0:
        traj = self.play(policy, max_steps=min(n_interactions, max_steps))
        new_trajectories.append(traj)
        n_interactions -= len(traj) - 1  # The initial reset does not count.
    else:
      raise ValueError(
          'Either n_trajectories or n_interactions must be defined.'
      )

    # Calculate returns.
    returns = [t.total_return for t in new_trajectories]
    if returns:
      mean_returns = sum(returns) / float(len(returns))
    else:
      mean_returns = 0

    # If we're only evaluating, we're done, return the average.
    if only_eval:
      return mean_returns
    # Store new trajectories.
    if new_trajectories:
      self._trajectories[epoch_id].extend(new_trajectories)

    # Mark that epoch epoch_id has changed.
    if epoch_id in self._saved_epochs_unchanged:
      self._saved_epochs_unchanged = [e for e in self._saved_epochs_unchanged
                                      if e != epoch_id]

    # Remove epochs not intended to be in the buffer
    current_trajectories = {
        key: value for key, value in self._trajectories.items()
        if key >= epoch_id - self._n_replay_epochs}
    self._trajectories = collections.defaultdict(list)
    self._trajectories.update(current_trajectories)

    # Update statistics.
    self._n_trajectories += len(new_trajectories)
    self._n_interactions += sum([len(traj) for traj in new_trajectories])

    return mean_returns

  def n_trajectories(self, epochs=None):
    # TODO(henrykm) support selection of epochs if really necessary (will
    # require a dump of a list of lengths in save_to_file
    del epochs
    return self._n_trajectories

  def n_interactions(self, epochs=None):
    # TODO(henrykm) support selection of epochs if really necessary (will
    # require a dump of a list of lengths in save_to_file
    del epochs
    return self._n_interactions

  def remove_epoch(self, epoch):
    """Useful when we need to remove an unwanted trajectory."""
    if epoch in self._trajectories:
      self._trajectories.pop(epoch)

  def trajectory_stream(self, epochs=None, max_slice_length=None,
                        sample_trajectories_uniformly=False, margin=0):
    """Return a stream of random trajectory slices from the specified epochs.

    Args:
      epochs: a list of epochs to use; we use all epochs if None
      max_slice_length: maximum length of the slices of trajectories to return
      sample_trajectories_uniformly: whether to sample trajectories uniformly,
        or proportionally to the number of slices in each trajectory (default)
      margin: number of extra steps after "done" that should be included in
        slices, so that networks see the terminal states in the training data

    Yields:
      random trajectory slices sampled uniformly from all slices of length
      up to max_slice_length in all specified epochs
    """
    # TODO(lukaszkaiser): add option to sample from n last trajectories.
    def n_slices(t):
      """How many slices of length upto max_slice_length in a trajectory."""
      if not max_slice_length:
        return 1
      # A trajectory [a, b, c, end_state] will have 2 slices of length 2:
      # the slice [a, b] and the one [b, c], with margin=0; 3 with margin=1.
      return max(1, len(t) + margin - max_slice_length)

    while True:
      all_epochs = list(self._trajectories.keys())
      max_epoch = max(all_epochs) + 1
      # Bind the epoch indices to a new name so they can be recalculated every
      # epoch.
      epoch_indices = epochs or all_epochs
      epoch_indices = [
          # So -1 means "last".
          ep % max_epoch for ep in epoch_indices
      ]
      # Remove duplicates and consider only epochs where some trajectories
      # were recorded.
      epoch_indices = [epoch_id for epoch_id in list(set(epoch_indices))
                       if self._trajectories[epoch_id]]

      # Sample an epoch proportionally to number of slices in each epoch.
      if len(epoch_indices) == 1:  # Skip this step if there's just 1 epoch.
        epoch_id = epoch_indices[0]
      else:
        # NOTE: Bottleneck. TODO(pkozakowski): Optimize.
        slices_per_epoch = [sum([n_slices(t) for t in self._trajectories[ep]])
                            for ep in epoch_indices]
        epoch_id = _sample_proportionally(epoch_indices, slices_per_epoch)
      epoch = self._trajectories[epoch_id]

      # Sample a trajectory proportionally to number of slices in each one.
      if sample_trajectories_uniformly:
        slices_per_trajectory = [1] * len(epoch)
      else:
        # NOTE: Bottleneck. TODO(pkozakowski): Optimize.
        slices_per_trajectory = [n_slices(t) for t in epoch]
      trajectory = _sample_proportionally(epoch, slices_per_trajectory)

      # Sample a slice from the trajectory.
      slice_start = np.random.randint(n_slices(trajectory))

      # Convert the whole trajectory to Numpy while adding the margin. The
      # result is cached, so we don't have to repeat this for every sample.
      trajectory_np = trajectory.to_np(margin, self._timestep_to_np)

      # Slice and yield the result.
      slice_end = slice_start + (
          max_slice_length or trajectory_np.observations.shape[0]
      )
      yield fastmath.nested_map(
          lambda x: x[slice_start:slice_end], trajectory_np
      )

  def trajectory_batch_stream(self, batch_size, epochs=None,
                              max_slice_length=None,
                              min_slice_length=None,
                              margin=0,
                              sample_trajectories_uniformly=False):
    """Return a stream of trajectory batches from the specified epochs.

    This function returns a stream of tuples of numpy arrays (tensors).
    If tensors have different lengths, they will be padded by 0.

    Args:
      batch_size: the size of the batches to return
      epochs: a list of epochs to use; we use all epochs if None
      max_slice_length: maximum length of the slices of trajectories to return
      min_slice_length: minimum length of the slices of trajectories to return
      margin: number of extra steps after "done" that should be included in
        slices, so that networks see the terminal states in the training data
      sample_trajectories_uniformly: whether to sample trajectories uniformly,
       or proportionally to the number of slices in each trajectory (default)

    Yields:
      batches of trajectory slices sampled uniformly from all slices of length
      at least min_slice_length and up to max_slice_length in all specified
      epochs
    """
    def pad(tensor_list):
      # Replace Nones with valid tensors.
      not_none_tensors = [t for t in tensor_list if t is not None]
      assert not_none_tensors, 'All tensors to pad are None.'
      prototype = np.zeros_like(not_none_tensors[0])
      tensor_list = [t if t is not None else prototype for t in tensor_list]

      max_len = max([t.shape[0] for t in tensor_list])
      if min_slice_length is not None:
        max_len = max(max_len, min_slice_length)
      min_len = min([t.shape[0] for t in tensor_list])
      if max_len == min_len:  # No padding needed.
        return np.array(tensor_list)

      pad_len = 2**int(np.ceil(np.log2(max_len)))
      return np.array([_zero_pad(t, (0, pad_len - t.shape[0]), axis=0)
                       for t in tensor_list])
    cur_batch = []
    for t in self.trajectory_stream(
        epochs, max_slice_length, sample_trajectories_uniformly, margin=margin
    ):
      cur_batch.append(t)
      if len(cur_batch) == batch_size:
        # bottleneck
        # zip(*cur_batch) transposes (batch_size, fields)
        # -> (fields, batch_size). Then we build TrajectoryNp from the fields.
        # Fields are observations, actions, ...
        batch_trajectory_np = TrajectoryNp(*zip(*cur_batch))
        # Actions, rewards and returns in the trajectory slice have shape
        # [batch_size, trajectory_length], which we denote as [B, L].
        # Observations are more complex: [B, L] + S, where S is the shape of the
        # observation space (self.observation_space.shape).
        # We stop the recursion at level 1, so we pass lists of arrays into
        # pad().
        yield fastmath.nested_map(pad, batch_trajectory_np, level=1)
        cur_batch = []
