# Copyright 2015 Google Inc. All Rights Reserved.
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
# ==============================================================================

"""Tests for Debugger Session."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import threading
import time

import numpy as np
import six
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

from tensorflow.core.lib.core import error_codes_pb2
from tensorflow.core.protobuf import config_pb2
from tensorflow.python.client import debugger
from tensorflow.python.client import session
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_util
from tensorflow.python.framework import test_util
from tensorflow.python.framework import versions
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import constant_op
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import state_ops
from tensorflow.python.ops import variables
from tensorflow.python.platform import googletest
from tensorflow.python.util import compat


# NOTE(mrry): Dummy shape registration for op used in the tests.
ops.RegisterShape('ConstructionFails')(None)


class DebugSessionTest(test_util.TensorFlowTestCase):

  def setUp(self):
    # TODO(cais): Proper mutex locking to push down to
    self._init_delay_sec = 0.1
    self._step_delay_sec = 0.02

  def _auto_step(self, debug_round, do_inspect=True, val_replace=None):
    """Automatically step through a debug session, with options.

    Because _auto_step uses step(), it is not affected by breakpoints in the
    debug_round object.

    Args:
      debug_round: A DebugRound object.
      do_inspect: Inspect the values during stepping, this will lead to a return
        value that equals the result of the execution.
      val_replace: A dictionary for node value injection. The keys are the node
        names. The values are callables that take one input argument (old node
        value) and returns a new node value that is injected to the node
        specified by the corresponding dict key once the node has just finished
        executing.

    Returns:
      If do_inspect == True, the result of the graph execution.
    """

    if not do_inspect and val_replace is not None:
      raise ValueError("val_replace cannot be performed if do_inspect is set "
                       "to False")

    result = None
    while True:
      debug_round.step()

      node_order = debug_round.query_node_order()
      node_idx = debug_round.where()
      is_complete = debug_round.is_complete()

      node_just_completed = node_order[node_idx]

      if do_inspect:
        node_val = debug_round.inspect_value(node_just_completed)
        if node_val is not None:
          result = node_val

        if val_replace is not None and node_just_completed in val_replace:
          replace_func = val_replace[node_just_completed]
          new_val = replace_func(node_val)

          debug_round.inject_value(new_val)

      if is_complete:
        debug_round.step()
        break

    return result

  def testConstantAddingSingleSteps(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(6.0, shape=[1, 1], name="tphass_a")
      b = constant_op.constant(7.0, shape=[1, 1], name="tphass_b")
      s = math_ops.add(a, b, name="tphass_s")

      # Create a DebugRound object
      debug_round = debugger.DebugRound(debug_sess, s)

      node_order = debug_round.query_node_order()
      self.assertTrue(isinstance(node_order, list))
      num_nodes = len(node_order)

      curr_pos = debug_round.where()
      self.assertEquals(0, curr_pos)

      while True:
        debug_round.step()

        # Verify that stepping causes the "where index" to increment properly
        node_idx = debug_round.where()
        self.assertEquals(curr_pos + 1, node_idx)
        curr_pos = node_idx

        # Verify inspect_value returns correct values
        if node_order[curr_pos] == "tphass_a":
          node_value = debug_round.inspect_value("tphass_a")
          self.assertAllClose(np.array([[6.0]]), node_value)
        elif node_order[curr_pos] == "tphass_b":
          node_value = debug_round.inspect_value("tphass_b")
          self.assertAllClose(np.array([[7.0]]), node_value)
        elif node_order[curr_pos] == "tphass_s":
          node_value = debug_round.inspect_value("tphass_s")
          self.assertAllClose(np.array([[13.0]]), node_value)

        # Verify is_complete
        is_complete = debug_round.is_complete()
        self.assertEquals(curr_pos == num_nodes - 1, is_complete)

        node_just_completed = node_order[node_idx]
        print("Node just completed: %s" % node_just_completed)

        if is_complete:
          debug_round.step()
          break

  def testConstantAddingMultiSteps(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(6.0, shape=[1, 1], name="a")
      b = constant_op.constant(7.0, shape=[1, 1], name="b")
      s = math_ops.add(a, b, name="s")

      # Create a DebugRound object
      debug_round = debugger.DebugRound(debug_sess, s)

      node_order = debug_round.query_node_order()
      self.assertTrue(isinstance(node_order, list))
      num_nodes = len(node_order)

      curr_pos = debug_round.where()
      self.assertEquals(0, curr_pos)

      while True:
        debug_round.step(2)

        # Verify that stepping causes the "where index" to increment properly
        node_idx = debug_round.where()
        if curr_pos + 2 >= num_nodes:
          self.assertEquals(num_nodes - 1, node_idx)
        else:
          self.assertEquals(curr_pos + 2, node_idx)
        curr_pos = node_idx

        # Verify is_complete
        is_complete = debug_round.is_complete()
        self.assertEquals(curr_pos == num_nodes - 1, is_complete)

        node_just_completed = node_order[node_idx]

        if is_complete:
          debug_round.step()
          break

  def testConstantAddingContinue(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(6.0, shape=[1, 1], name="a")
      b = constant_op.constant(7.0, shape=[1, 1], name="b")
      s = math_ops.add(a, b, name="s")

      # Create a DebugRound object
      debug_round = debugger.DebugRound(debug_sess, s)

      node_order = debug_round.query_node_order()
      self.assertTrue(node_order.count("s") == 1)

      # Continue until node "s" has just finished executing
      debug_round.cont("s")

      # Verify that the debug breaks on "s"
      self.assertEquals(node_order.index("s"), debug_round.where())

      self._auto_step(debug_round)

  def testConstantAddingContinueToEnd(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(6.0, shape=[1, 1], name="a")
      b = constant_op.constant(7.0, shape=[1, 1], name="b")
      s = math_ops.add(a, b, name="s")

      # Create a DebugRound object
      debug_round = debugger.DebugRound(debug_sess, s)

      # Calling cont() without node_name specified should let the debug round
      # continue to the end
      debug_round.cont()

      # Verify that the debug breaks on the last node
      self.assertEquals(len(debug_round.query_node_order()) - 1,
                        debug_round.where())

      self._auto_step(debug_round)

  def testConstantAddingWithInjection(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(np.array([[6.0]]).astype(np.float32),
                               name="phawi_a")
      b = constant_op.constant(np.array([[7.0]]).astype(np.float32),
                               name="phawi_b")
      s = math_ops.add(a, b, name="phawi_s")

      # Create a DebugRound object
      debug_round = debugger.DebugRound(debug_sess, s)
      node_order = debug_round.query_node_order()
      num_nodes = len(node_order)
      self.assertEquals(0, debug_round.where())
      curr_pos = 0

      while True:
        debug_round.step()

        # Verify that stepping causes the "where index" to increment properly
        node_idx = debug_round.where()
        self.assertEquals(curr_pos + 1, node_idx)
        curr_pos = node_idx

        # Verify inspect_value returns correct values
        if node_order[curr_pos] == "phawi_a":
          node_value = debug_round.inspect_value("phawi_a")
          self.assertAllClose(np.array([[6.0]]), node_value)

          debug_round.inject_value(np.array([[60.0]]).astype(np.float32))
        elif node_order[curr_pos] == "phawi_b":
          node_value = debug_round.inspect_value("phawi_b")
          self.assertAllClose(np.array([[7.0]]), node_value)

          debug_round.inject_value(np.array([[70.0]]).astype(np.float32))
        elif node_order[curr_pos] == "phawi_s":
          node_value = debug_round.inspect_value("phawi_s")

          # The sum should reflect the two newly injected values
          self.assertAllClose(np.array([[130.0]]).astype(np.float32),
                              node_value)

        # Verify is_complete
        is_complete = debug_round.is_complete()
        self.assertEquals(curr_pos == num_nodes - 1, is_complete)

        node_just_completed = node_order[node_idx]

        if is_complete:
          debug_round.step()
          break

  def testVariablesWithInjection(self):
    with session.Session("debug") as debug_sess:
      A0 = np.array([[10.0]]).astype(np.float32)
      B0 = np.array([[20.0]]).astype(np.float32)

      A = variables.Variable(A0, name="vwi_A")
      B = variables.Variable(B0, name="vwi_B")

      aa = A.assign_add(B0)

      # Initialize variables
      init_A = A.initializer
      debug_round = debugger.DebugRound(debug_sess, init_A)
      self._auto_step(debug_round, do_inspect=False)

      init_B = B.initializer
      debug_round = debugger.DebugRound(debug_sess, init_B)
      self._auto_step(debug_round, do_inspect=False)

      # Perform calculation
      debug_round = debugger.DebugRound(debug_sess, aa)
      self._auto_step(debug_round)

      # Get the updated value of A
      debug_round = debugger.DebugRound(debug_sess, A)
      result = self._auto_step(debug_round)

      # The new value of A should now be A0 + B0, due to the assign_add op
      self.assertAllClose(A0 + B0, result)

      # Do it twice to test repeated value injection to the same node
      for i in xrange(2):
        # Now, run the assign_add op again, but replace A with the old (initial)
        # value.
        def inject_A(old_val):
          return A0
        injection = {"vwi_A": inject_A}

        debug_round = debugger.DebugRound(debug_sess, aa)
        result = self._auto_step(debug_round, val_replace=injection)

        # Get the updated value of A again
        debug_round = debugger.DebugRound(debug_sess, A)
        result = self._auto_step(debug_round)

        # Note: If it were not for the value injection, this would be equal to
        # A0 + 2 * B0 or A0 + 3 * B0 by now.
        self.assertAllClose(A0 + B0, result)

  def testNodeBreakpoint(self):
    with session.Session("debug") as debug_sess:
      M = constant_op.constant(
          np.array([[1.0, 2.0], [3.0, 4.0]]).astype(np.float32),
          name="nbp_M")
      Mt = array_ops.transpose(M, name="nbp_Mt")

      debug_round = debugger.DebugRound(debug_sess, Mt)

      node_order = debug_round.query_node_order()
      self.assertTrue(1, node_order.count("nbp_M"))

      # Insert a breakpoint after nbp_M
      bp_handle = debug_round.break_after("nbp_M")

      # Verify breakpoint getter
      node_bps, pred_bps = debug_round.get_breakpoints()
      self.assertEquals([bp_handle], node_bps)
      self.assertEquals({}, pred_bps)

      # cont() without arg (toward the end) should break at nbp_M
      debug_round.cont()
      self.assertEquals("nbp_M", node_order[debug_round.where()])

      # Finish the rest of the execution (if any)
      result = self._auto_step(debug_round)

      self.assertAllClose(np.array([[1.0, 3.0], [2.0, 4.0]]).astype(np.float32),
                          result)

  def testNodeBreakpoint(self):
    with session.Session("debug") as debug_sess:
      M = constant_op.constant(
          np.array([[1.0, 2.0], [3.0, 4.0]]).astype(np.float32),
          name="nbp_M")
      Mt = array_ops.transpose(M, name="nbp_Mt")

      debug_round = debugger.DebugRound(debug_sess, Mt)

      node_order = debug_round.query_node_order()
      self.assertTrue(1, node_order.count("nbp_M"))

      # Insert a breakpoint after nbp_M
      bp_handle = debug_round.break_after("nbp_M")

      # Verify breakpoint getter
      node_bps, pred_bps = debug_round.get_breakpoints()
      self.assertEquals([bp_handle], node_bps)
      self.assertEquals({}, pred_bps)

      # cont() without arg (toward the end) should break at nbp_M
      debug_round.cont()
      self.assertEquals("nbp_M", node_order[debug_round.where()])

      # Finish the rest of the execution (if any)
      result = self._auto_step(debug_round)

      self.assertAllClose(np.array([[1.0, 3.0], [2.0, 4.0]]).astype(np.float32),
                          result)

  def testBeforeNodeBreakpointRemoval(self):
    with session.Session("debug") as debug_sess:
      M = constant_op.constant(
          np.array([[1.0, 2.0], [3.0, 4.0]]).astype(np.float32),
          name="bfnbp_M")
      Mt = array_ops.transpose(M, name="bfnbp_Mt")

      debug_round = debugger.DebugRound(debug_sess, Mt)

      node_order = debug_round.query_node_order()
      self.assertEquals(1, node_order.count("bfnbp_Mt"))

      # Insert a breakpoint before bfnbp_Mt
      bp_handle = debug_round.break_before("bfnbp_Mt")

      # Verify breakpoint getter
      node_bps, pred_bps = debug_round.get_breakpoints()
      self.assertEquals([bp_handle], node_bps)
      self.assertEquals({}, pred_bps)

      debug_round.cont()

      # Verify that the debug round has broken at the node before bfnbp_Mt
      self.assertEquals(node_order.index("bfnbp_Mt") - 1, debug_round.where())

      # Finish the rest of the execution (if any)
      self._auto_step(debug_round)

  def testInvalidNodeBreakpoint(self):
    with session.Session("debug") as debug_sess:
      M = constant_op.constant(
          np.array([[1.0, 2.0], [3.0, 4.0]]).astype(np.float32),
          name="inbp_M")
      Mt = array_ops.transpose(M, name="inbp_Mt")

      debug_round = debugger.DebugRound(debug_sess, Mt)
      node_order = debug_round.query_node_order()

      with self.assertRaisesRegexp(ValueError, "does not exist"):
        debug_round.break_after("foo_bar_qux_baz")

      # Verify breakpoint getter
      node_bps, pred_bps = debug_round.get_breakpoints()
      self.assertEquals([], node_bps)
      self.assertEquals({}, pred_bps)

      # There is no valid breakpoint, so cont() should go till the end
      debug_round.cont()
      self.assertEquals(len(node_order) - 1, debug_round.where())

      # Finish the rest of the execution (if any)
      self._auto_step(debug_round)

  def testPredBreakpoint(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(np.array(11.0).astype(np.float32),
                               name="pbp_a")
      b = constant_op.constant(np.array(22.0).astype(np.float32),
                               name="pbp_b")
      s = math_ops.add(a, b, name="pbp_s")

      # This predicate is not expected to be met
      def pred1(node_name, node_val):
        return node_val > 5.0 and node_val < 6.0

      # This predicate is expected to be met after b and s
      def pred2(node_name, node_val):
        return node_val > 20.0

      debug_round = debugger.DebugRound(debug_sess, s)
      node_order = debug_round.query_node_order()

      bp_handle_1 = debug_round.break_if(pred1)
      bp_handle_2 = debug_round.break_if(pred2)

      # Verify breakpoint getter
      node_bps, pred_bps = debug_round.get_breakpoints()
      self.assertEquals([], node_bps)
      self.assertEquals(2, len(pred_bps))
      self.assertTrue(bp_handle_1 in pred_bps)
      self.assertTrue(bp_handle_2 in pred_bps)

      # First, the debug round should break at pbp_b
      debug_round.cont()
      self.assertEquals("pbp_b", node_order[debug_round.where()])

      # Second, the debug round should break at pbp_s
      debug_round.cont()
      self.assertEquals("pbp_s", node_order[debug_round.where()])
      s_val = debug_round.inspect_value("pbp_s")

      # Finish the rest of the execution (if any)
      result = self._auto_step(debug_round)
      self.assertAllClose(np.array(33.0).astype(np.float32), s_val)

  def testPredBreakpointRemoval(self):
    with session.Session("debug") as debug_sess:
      a = constant_op.constant(np.array(11.0).astype(np.float32),
                               name="pbpr_a")
      b = constant_op.constant(np.array(22.0).astype(np.float32),
                               name="pbpr_b")
      s = math_ops.add(a, b, name="pbpr_s")

      # This predicate is not expected to be met
      def pred1(node_name, node_val):
        return node_val > 5.0 and node_val < 6.0

      # This predicate is expected to be met after b and s
      def pred2(node_name, node_val):
        return node_val > 20.0

      debug_round = debugger.DebugRound(debug_sess, s)
      node_order = debug_round.query_node_order()

      bp_handle_1 = debug_round.break_if(pred1)
      bp_handle_2 = debug_round.break_if(pred2)

      # Remove pred2. This should lead to no breaking in debug round's cont().
      debug_round.remove_breakpoint(bp_handle_2)

      # Verify breakpoint getter
      node_bps, pred_bps = debug_round.get_breakpoints()
      self.assertEquals([], node_bps)
      self.assertEquals(1, len(pred_bps))
      self.assertTrue(bp_handle_1 in pred_bps)
      self.assertFalse(bp_handle_2 in pred_bps)

      debug_round.cont()
      self.assertEquals(len(node_order) - 1, debug_round.where())

      # Finish the rest of the execution (if any)
      result = self._auto_step(debug_round)


if __name__ == '__main__':
  googletest.main()
