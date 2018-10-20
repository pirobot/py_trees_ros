#!/usr/bin/env python
#
# License: BSD
#   https://raw.github.com/stonier/py_trees_ros/license/LICENSE
#
##############################################################################
# Documentation
##############################################################################

"""
The :class:`ROS Behaviour Tree <py_trees_ros.trees.BehaviourTree>`
extends the core :class:`Behaviour Tree <py_trees.trees.BehaviourTree>` class
with a few ROS style adornments. The major features currently include:

* Publishers for ascii/dot tree visualisations and the blackboard
* A publisher which dumps the entire tree at every change for the rqt plugin
* Bagging of the tree for offline visualisation and debugging
"""

##############################################################################
# Imports
##############################################################################

import datetime
import os
import py_trees
import py_trees.console as console
import py_trees_msgs.msg as py_trees_msgs
# import rosbag
# TODO: import rospkg
import rclpy
import std_msgs.msg as std_msgs
import threading
import unique_id

from . import blackboard
from . import conversions
from . import utilities
from . import visitors

##############################################################################
# ROS Trees
##############################################################################


class BehaviourTree(py_trees.trees.BehaviourTree):
    """
    Extend the :class:`py_trees.trees.BehaviourTree` class with
    a few bells and whistles for ROS:

    * ros publishers with snapshot ascii/dot graph views of the tree
    * ros publisher with data representation of the entire tree for monitoring/bagging
    * ros publisher showing what the current tip is
    * a blackboard exchange with introspection and watcher services


    ROS Publishers:
        * **~ascii/tree** (:class:`std_msgs.msg.String`)

          * static view of the entire tree (debugging)
        * **~ascii/snapshot** (:class:`std_msgs.msg.String`)

          * runtime ascii snapshot view of the ticking tree (debugging)
        * **~dot/tree** (:class:`std_msgs.msg.String`)

          * static dot graph of the entire tree (debugging)
        * **~log/tree** (:class:`py_trees_msgs.msg.BehaviourTree`)

          * representation of the entire tree in message form for rqt/bagging
        * **~tip** (:class:`py_trees_msgs.msg.Behaviour`)

          * the tip of the tree after the last tick

    .. seealso::
        It also exposes publishers and services from the blackboard exchange
        in it's private namespace. Refer to :class:`~py_trees_ros.blackboard.Exchange` for details.

    Args:
        root (:class:`~py_trees.behaviour.Behaviour`): root node of the tree

    Raises:
        AssertionError: if incoming root variable is not the correct type
    """
    def __init__(self, root):
        """
        Initialise the tree with a root.

        :param root: root node of the tree.
        :type root: instance or descendant of :py:class:`Behaviour <py_trees.behaviours.Behaviour>`
        :raises AssertionError: if incoming root variable is not the correct type.
        """
        super(BehaviourTree, self).__init__(root)
        self.snapshot_visitor = visitors.SnapshotVisitor()
        self.logging_visitor = visitors.LoggingVisitor()
        self.visitors.append(self.snapshot_visitor)
        self.visitors.append(self.logging_visitor)
        self._bag_closed = False

        now = datetime.datetime.now()
        topdir = utilities.get_py_trees_home()
        subdir = os.path.join(topdir, now.strftime('%Y-%m-%d'))
        if not os.path.exists(topdir):
            os.makedirs(topdir)
        if not os.path.exists(subdir):
            os.makedirs(subdir)

        # opens in ros home directory for the user
        # TODO: self.bag = rosbag.Bag(subdir + '/behaviour_tree_' + now.strftime("%H-%M-%S") + '.bag', 'w')

        self.last_tree = py_trees_msgs.BehaviourTree()
        self.lock = threading.Lock()

        # delay ROS specific artifacts so we can create/introsepct on this class
        # without having to go live.
        self.node = None
        self.publishers = None

        # _cleanup must come last as it assumes the existence of the bag
        # TODO: rospy.on_shutdown(self._cleanup)

    def setup(self, timeout):
        """
        Setup the publishers, exechange and add ros-relevant pre/post tick handlers to the tree.
        Ultimately relays this call down to all the behaviours in the tree.

        Args:
            timeout (:obj:`float`): time to wait (0.0 is blocking forever)

        Returns:
            :obj:`bool`: suceess or failure of the operation
        """
        default_node_name = "tree"
        try:
            self.node = rclpy.create_node(default_node_name)
        except rclpy.exceptions.NotInitializedException:
            print(console.red + "ERROR: rlcpy not yet initialised [{}]".format(default_node_name) + console.reset)
            return False
        self._setup_publishers()
        self.blackboard_exchange = blackboard.Exchange()
        if not self.blackboard_exchange.setup(self.node, timeout):
            return False
        self.post_tick_handlers.append(self._publish_tree_snapshots)
        self.post_tick_handlers.append(self.blackboard_exchange.publish_blackboard)
        return super(BehaviourTree, self).setup(timeout)

    def _setup_publishers(self):
        latched = True
        self.publishers = utilities.Publishers(self.node,
            [
                ("ascii_tree", "~/ascii/tree", std_msgs.String, latched, 2),
                ("ascii_snapshot", "~/ascii/snapshot", std_msgs.String, latched, 2),
                ("dot_tree", "~/dot/tree", std_msgs.String, latched, 2),
                ("log_tree", "~/log/tree", py_trees_msgs.BehaviourTree, latched, 2),
                ("tip", "~/tip", py_trees_msgs.Behaviour, latched, 2)
            ]
        )

        # publish current state
        # TODO: self._publish_tree_modifications(self.root)
        # set a handler to publish future modifiactions
        # tree_update_handler is in the base class, set this to the callback function here.
        self.tree_update_handler = self._publish_tree_modifications

    def _publish_tree_modifications(self, tree):
        """
        Publishes updates when the whole tree has been modified.

        This function is passed in as a visitor to the underlying behaviour tree and triggered
        when there has been a change.
        """
        if self.publishers is None:
            rospy.logerr("BehaviourTree: call setup() on this tree to initialise the ros components")
            return
        self.publishers.ascii_tree.publish(std_msgs.String(py_trees.display.ascii_tree(self.root)))
        self.publishers.dot_tree.publish(std_msgs.String(py_trees.display.stringify_dot_tree(self.root)))

    def _publish_tree_snapshots(self, tree):
        """
        Callback that runs on a :class:`BehaviourTree <py_trees.trees.BehaviourTree>` after
        it has ticked.

        :param tree: the behaviour tree
        :type tree: :py:class:`BehaviourTree <py_trees.trees.BehaviourTree>`
        """
        if self.publishers is None:
            rospy.logerr("BehaviourTree: call setup() on this tree to initialise the ros components")
            return
        snapshot = "\n\n%s" % py_trees.display.ascii_tree(self.root, snapshot_information=self.snapshot_visitor)
        self.publishers.ascii_snapshot.publish(std_msgs.String(snapshot))

        for behaviour in self.logging_visitor.tree.behaviours:
            behaviour.is_active = True if unique_id.fromMsg(behaviour.own_id) in self.snapshot_visitor.nodes else False
        # We're not interested in sending every single tree - only send a
        # message when the tree changes.
        if self.logging_visitor.tree.behaviours != self.last_tree.behaviours:
            if self.root.tip() is None:
                rospy.logerr("Behaviours: your tree is returning in an INVALID state (should always be FAILURE, RUNNING or SUCCESS)")
                return
            self.publishers.tip.publish(conversions.behaviour_to_msg(self.root.tip()))
            self.publishers.log_tree.publish(self.logging_visitor.tree)
            with self.lock:
                if not self._bag_closed:
                    # self.bag.write(self.publishers.log_tree.name, self.logging_visitor.tree)
                    pass
            self.last_tree = self.logging_visitor.tree

    def _cleanup(self):
        with self.lock:
            # self.bag.close()
            self.interrupt_tick_tocking = True
            self._bag_closed = True
