# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""DNA generators using evolutionary algorithms."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import random
import time
import typing
from absl import logging

from ops import LossOpNode
from ops import Node
from program import build_program
from program import get_possible_input_idxs
from program import sample_valid_program_spec
from program_search import ProgramSpec
import numpy as np
import pyglove as pg


@pg.members([
    ('mutation_probability', pg.typing.Float(default=0.8),
     'Probability that a mutation will take place.'),
    ('input_nodes', pg.typing.List(pg.typing.Object(Node)), 'Input Nodes'),
    ('num_freeze_ops', pg.typing.Int(), 'Num ops to freeze'),
])
class DAGPointMutator(pg.evolution.Mutator):
  """A class to carry out a point mutation in the DAG search space."""

  allow_symbolic_assignment = True

  def _on_bound(self):
    # Dict for saving hashed outputs of a program and its reward to test for
    # duplicates.
    self.output_dict: typing.Dict[float, float] = {}  # pytype: disable=invalid-annotation  # attribute-variable-annotations
    # Initialize random set of inputs to test for duplicates.
    self.random_inputs = [
        x.initialize_random_input(10) for x in self.input_nodes
    ]
    self.dna_hash = {}
    self.rand_float = np.random.uniform()
    # self._random = random.Random(self.seed)

  def _compute_hash_output(self, program) -> float:
    output = program(*self.random_inputs)
    output_str = sum([round(x, 4) for x in output.numpy().ravel()])
    if np.isnan(output_str) or np.isinf(output_str):
      output_str = self.rand_float
    output_str = float(
        np.clip(output_str,
                np.finfo(np.float32).min,
                np.finfo(np.float32).max))
    return output_str

  def _check_duplicate(self, program):
    output_str = self._compute_hash_output(program)
    if output_str in self.output_dict:
      return output_str, True, self.output_dict[output_str]
    else:
      return output_str, False, 0.

  def update_output_dict(self, dna, reward):
    """Called by evolution controller to save reward of program."""
    output_str = dna.to_json(compact=True)['value'][-1]
    if output_str in self.output_dict:
      logging.info('Duplicate already in dict %s', output_str)
    self.output_dict[output_str] = reward

  def mutate(self, dna: pg.DNA) -> pg.DNA:  # pytype: disable=signature-mismatch  # overriding-parameter-count-checks
    """Mutates a DNA by randomizing one of the operations."""
    old_spec = dna.spec
    # program_spec = self.template.decode(dna)
    if random.random() < self.mutation_probability:
      t0 = time.time()
      program_spec = self.template.decode(dna)
      t1 = time.time()
      logging.info('Decoding took %s', t1 - t0)
      program_lst = program_spec.program_lst
      loss_program, _ = build_program(self.input_nodes, program_spec,
                                      self.operators, 0)

      while True:
        new_program_spec = self._alter_node_idx(dna, program_lst, loss_program)
        new_loss_program, valid = build_program(self.input_nodes,
                                                new_program_spec,
                                                self.operators, 0)
        if not valid:
          continue
        t4 = time.time()
        hash_string, duplicate, reward = self._check_duplicate(new_loss_program)
        t5 = time.time()
        logging.info('Duplicate check took %s', t5 - t4)
        new_dna = self.template.encode(
            ProgramSpec(
                program_lst=new_program_spec.program_lst,
                duplicate=duplicate,
                reward=float(reward),
                loss_weight=new_program_spec.loss_weight,
                hash_output=hash_string))
        t6 = time.time()
        logging.info('Encoding took %s', t6 - t5)
        logging.info('%s, %s', program_spec.loss_weight,
                     new_program_spec.loss_weight)
        if valid:
          new_dna.use_spec(old_spec)
          logging.info('use spec took %s', time.time() - t6)
          return new_dna
    else:
      # Sample new program
      program_spec = sample_valid_program_spec(
          self.input_nodes,
          self.existing_ops,
          self.program_length,
          self.operators,
          self.adjust_loss_weight,
      )
      dna = self.template.encode(program_spec)
      dna.use_spec(old_spec)
      return dna

  def _sample_node_idx_to_mutate(self,
                                 program_lst,
                                 loss_program,
                                 only_leaf=False):
    idxs = list(
        range(
            len(self.input_nodes) + self.num_freeze_ops,
            len(loss_program.ops_lst)))

    if not self.adjust_loss_weight:
      del idxs[-1]
    node_idx = random.choice(idxs)
    n_idx = node_idx - len(self.input_nodes)
    op_to_mutate = loss_program.ops_lst[node_idx]

    logging.info('Mutating op %s', op_to_mutate)

    return node_idx, n_idx, op_to_mutate

  def _alter_node_idx(self, dna, program_lst, loss_program):
    node_idx, n_idx, op_to_replace = self._sample_node_idx_to_mutate(
        program_lst, loss_program)

    if isinstance(op_to_replace, LossOpNode):
      return self._alter_continuous(dna, program_lst, loss_program)

    is_leaf = node_idx in loss_program.find_leaf_nodes()

    # Get all possible inputs
    all_inputs = loss_program.ops_lst[:node_idx - 1]
    # Find all possible ops and inputs to switch at node idx
    possible_ops = list(set(self.operators) - set([LossOpNode]))
    valid_lst = []
    count = 0
    t0 = time.time()
    for op_cls in possible_ops:
      op_idx = self.operators.index(op_cls)
      for input_idxs in get_possible_input_idxs(op_cls, range(len(all_inputs))):
        count += 1
        input_ops = [all_inputs[idx] for idx in input_idxs]
        input_dtypes = [x.odtype for x in input_ops]
        if op_cls.precheck_valid_input(input_ops):
          # If leaf node, then output dtype can be anything.
          if is_leaf:
            valid_lst.append((op_idx, list(input_idxs)))
          # Otherwise check that output dtype is same.
          else:
            new_op = op_cls(
                input_idxs=list(input_idxs), input_dtypes=input_dtypes)
            if new_op.odtype == op_to_replace.odtype:
              valid_lst.append((op_idx, list(input_idxs)))
    t1 = time.time()
    logging.info('Tried %d possible choices', count)
    logging.info('Took time %s ', t1 - t0)
    op_idx, input_idxs = random.choice(valid_lst)
    new_program_lst = []
    for i, (old_idx, old_input_idxs) in enumerate(program_lst):
      if i == n_idx:
        new_program_lst.append((op_idx, input_idxs))
      else:
        new_program_lst.append((old_idx, old_input_idxs))
    return ProgramSpec(program_lst=new_program_lst)

  def _alter_continuous(self, dna, program_lst, loss_program):
    return ProgramSpec(
        program_lst=program_lst,
        loss_weight=random.choice(loss_program.ops_lst[-1].loss_weights))


@pg.members([
    ('population_size', pg.typing.Int(min_value=2)),
    ('tournament_size', pg.typing.Int(min_value=1)),
    ('mutator', pg.typing.Object(pg.evolution.Mutator)),
    ('seed', pg.typing.Int().noneable()),
])
class CGSRegularizedEvolution(pg.DNAGenerator):
  """Regularized Evolution algorithm.

  For reference and for citations, please use:
  https://www.aaai.org/ojs/index.php/AAAI/article/view/4405
  """

  def setup(self, dna_spec: pg.DNASpec):
    """Sets up the generator."""
    super().setup(dna_spec)
    assert self.tournament_size >= 2
    assert self.population_size >= self.tournament_size
    self._population = collections.deque()
    if self.seed is None:
      self._random = random
    else:
      self._random = random.Random(self.seed)

  def propose(self) -> pg.DNA:
    """Proposes a DNA."""
    if len(self._population) < self.population_size:
      # Initialize population with random DNAs.
      program_spec = sample_valid_program_spec(self.mutator.input_nodes,
                                               self.mutator.existing_ops,
                                               self.mutator.program_length,
                                               self.mutator.operators,
                                               self.mutator.adjust_loss_weight)
      dna = self.mutator.template.encode(program_spec)
      dna.use_spec(self.dna_spec)
      return dna
    else:
      # Select with a tournament. See:
      # Goldberg, D. E., and Deb, K. 1991, "A comparative analysis of selection
      # schemes used in genetic algorithms", FOGA.
      assert len(self._population) == self.population_size
      tournament = self._random.sample(self._population, self.tournament_size)
      for individual in tournament:
        assert individual.reward is not None
      selected = max(tournament, key=lambda i: i.reward)

      # Mutate.
      parent = selected.dna.clone(deep=True)
      parent.use_spec(self.dna_spec)
      return self.mutator.mutate(parent)

  def feedback(  # pytype: disable=signature-mismatch  # overriding-parameter-type-checks
      self, dna: pg.DNA, reward: float) -> None:
    """Feeds back information about an evaluated DNA to the search algorithm."""
    self.mutator.update_output_dict(dna, reward)
    self._population.append(_Individual(dna, reward))
    while len(self._population) > self.population_size:
      # Remove oldest.
      self._population.popleft()


class _Individual(object):
  """Represents an individual.

  An individual contains a DNA and its corresponding reward after being
  evaluated once. Used by the RegularizedEvolution algorithm.
  """

  def __init__(self, dna, reward):
    self._dna = dna
    assert reward is not None
    self._reward = reward

  @property
  def dna(self):
    return self._dna

  @property
  def reward(self):
    return self._reward
