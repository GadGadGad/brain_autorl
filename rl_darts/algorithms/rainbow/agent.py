"""RainbowDQN agent implementation."""
import copy
from typing import Optional

from acme import datasets
from acme import specs
from acme.adders import reverb as adders
from acme.agents import agent
from acme.agents.tf import actors
from acme.tf import savers as tf2_savers
from acme.tf import utils as tf2_utils
from acme.utils import loggers

from brain_autorl.rl_darts.algorithms.rainbow import learning

import reverb
import sonnet.v2 as snt
import tensorflow as tf
import trfl


class RainbowDQN(agent.Agent):
  """DQN agent.

  This implements a single-process DQN agent. This is a simple Q-learning
  algorithm that inserts N-step transitions into a replay buffer, and
  periodically updates its policy by sampling these transitions using
  prioritization.
  """

  def __init__(
      self,
      environment_spec: specs.EnvironmentSpec,
      network: snt.Module,
      batch_size: int = 256,
      prefetch_size: int = 4,
      target_update_period: int = 100,
      samples_per_insert: float = 32.0,
      min_replay_size: int = 1000,
      max_replay_size: int = 1000000,
      importance_sampling_exponent: float = 0.2,
      priority_exponent: float = 0.6,
      n_step: int = 5,
      epsilon: Optional[tf.Tensor] = None,
      learning_rate: float = 1e-3,
      discount: float = 0.99,
      logger: Optional[loggers.Logger] = None,
      checkpoint: bool = True,
      checkpoint_subpath: str = '~/acme/',
      policy_network: Optional[snt.Module] = None,
      max_gradient_norm: Optional[float] = None,
      default_priority: float = 5.0,
  ):
    """Initialize the agent.

    Args:
      environment_spec: description of the actions, observations, etc.
      network: the online Q network (the one being optimized)
      batch_size: batch size for updates.
      prefetch_size: size to prefetch from replay.
      target_update_period: number of learner steps to perform before updating
        the target networks.
      samples_per_insert: number of samples to take from replay for every insert
        that is made.
      min_replay_size: minimum replay size before updating. This and all
        following arguments are related to dataset construction and will be
        ignored if a dataset argument is passed.
      max_replay_size: maximum replay size.
      importance_sampling_exponent: power to which importance weights are raised
        before normalizing.
      priority_exponent: exponent used in prioritized sampling.
      n_step: number of steps to squash into a single transition.
      epsilon: probability of taking a random action; ignored if a policy
        network is given.
      learning_rate: learning rate for the q-network update.
      discount: discount to use for TD updates.
      logger: logger object to be used by learner.
      checkpoint: boolean indicating whether to checkpoint the learner.
      checkpoint_subpath: directory for the checkpoint.
      policy_network: if given, this will be used as the policy network.
        Otherwise, an epsilon greedy policy using the online Q network will be
        created. Policy network is used in the actor to sample actions.
      max_gradient_norm: used for gradient clipping.
      default_priority: When new items are added to the replay buffer, use this
        priority. Use a value > 1.0 helps to sample new experiences sooner.
    """

    # Create a replay server to add data to. This uses no limiter behavior in
    # order to allow the Agent interface to handle it.
    replay_table = reverb.Table(
        name=adders.DEFAULT_PRIORITY_TABLE,
        sampler=reverb.selectors.Prioritized(priority_exponent),
        remover=reverb.selectors.Fifo(),
        max_size=max_replay_size,
        rate_limiter=reverb.rate_limiters.MinSize(1),
        signature=adders.NStepTransitionAdder.signature(environment_spec))
    self._server = reverb.Server([replay_table], port=None)

    # The adder is used to insert observations into replay.
    address = f'localhost:{self._server.port}'
    adder = adders.NStepTransitionAdder(
        client=reverb.Client(address),
        n_step=n_step,
        discount=discount,
        priority_fns={
            adders.DEFAULT_PRIORITY_TABLE: lambda x: default_priority
        })

    # The dataset provides an interface to sample from replay.
    replay_client = reverb.TFClient(address)
    dataset = datasets.make_reverb_dataset(
        server_address=address,
        batch_size=batch_size,
        prefetch_size=prefetch_size)

    # Create epsilon greedy policy network by default.
    if policy_network is None:
      # Use constant 0.05 epsilon greedy policy by default.
      if epsilon is None:
        epsilon = tf.Variable(0.05, trainable=False)
      policy_network = snt.Sequential([
          network,
          # q[0] gives q-values in the distribution head.
          lambda q: trfl.epsilon_greedy(q[0], epsilon=epsilon).sample(),
      ])

    # Create a target network.
    target_network = copy.deepcopy(network)

    # Ensure that we create the variables before proceeding (maybe not needed).
    tf2_utils.create_variables(network, [environment_spec.observations])
    tf2_utils.create_variables(target_network, [environment_spec.observations])

    # Create the actor which defines how we take actions.
    actor = actors.FeedForwardActor(policy_network, adder)

    # The learner updates the parameters (and initializes them).
    learner = learning.RainbowDQNLearner(
        network=network,
        target_network=target_network,
        discount=discount,
        importance_sampling_exponent=importance_sampling_exponent,
        learning_rate=learning_rate,
        target_update_period=target_update_period,
        dataset=dataset,
        replay_client=replay_client,
        max_gradient_norm=max_gradient_norm,
        logger=logger,
        checkpoint=checkpoint)

    if checkpoint:
      self._checkpointer = tf2_savers.Checkpointer(
          directory=checkpoint_subpath,
          objects_to_save=learner.state,
          subdirectory='dqn_learner',
          max_to_keep=2,
          time_delta_minutes=60.)
    else:
      self._checkpointer = None

    super().__init__(
        actor=actor,
        learner=learner,
        min_observations=max(batch_size, min_replay_size),
        observations_per_step=float(batch_size) / samples_per_insert)

  def update(self):
    super().update()
    if self._checkpointer is not None:
      self._checkpointer.save()
