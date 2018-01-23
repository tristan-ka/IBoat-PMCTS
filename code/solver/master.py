#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import numpy as np
from utils import Hist
import matplotlib.pyplot as plt
from worker import UCT_COEFF
from math import log, sin, cos, pi
from matplotlib import animation
import pickle

sys.path.append('../model/')
from simulatorTLKT import ACTIONS, Simulator, A_DICT


class MasterTree:
    """
    Master tree that manages ns different WorkerTree in parallel.
    Each WorkerTree is searching on a different Weather scenario.

    :ivar dict nodes: dictionary containing `MasterNode`, the keys are their corresponding hash
    :ivar numpy.array probability: array containing the probability of each scenario
    :ivar list Simulators: List of the simulators used during the search
    :ivar int max_depth: Maximum depth of the master tree computed after `MasterTree.get_depth()` has been called
    :ivar int numScenarios: Number of scenarios
    :ivar list destination: Destination state [lat, long]
    :ivar list best_global_policy:

    """

    def __init__(self, sims, destination):
        num_scenarios = len(sims)
        self.nodes = dict()
        self.Simulators = sims
        self.probability = np.array([1 / num_scenarios for _ in range(num_scenarios)])
        self.nodes[hash(tuple([]))] = MasterNode(num_scenarios, nodehash=hash(tuple([])))
        self.max_depth = None
        self.numScenarios = num_scenarios
        self.destination = destination
        self.best_global_policy = []
        self.best_global_nodes_policy = []
        self.best_policy = dict()
        self.best_nodes_policy = dict()

    def integrate_buffer(self, buffer):
        """
        Integrates a list of update from a scenario. This method is to be called from a worker.
        :param buffer: list of updates coming from the worker. One update is a list :\
            [scenarioId, newNodeHash, parentHash, action, reward]
        :type buffer: list of list
        """
        for update in buffer:
            scenarioId, newNodeHash, parentHash, action, reward = update
            node = self.nodes.get(newNodeHash, 0)
            if node == 0:
                self.nodes[newNodeHash] = MasterNode(self.numScenarios, nodehash=newNodeHash,
                                                     parentNode=self.nodes[parentHash], action=action)
                node = self.nodes[newNodeHash]

            node.add_reward(scenarioId, reward)
            node.backup(scenarioId, reward)

    def update(self, worker_dict, event_dict, finish_event_dict):
        """
        Background task which waits for worker buffer updates
        :param worker_dict:
        :param event_dict:
        :param finish_event_dict:
        :return:
        """
        stop = False
        while not stop:
            for i, event in enumerate(event_dict.values()):
                # If a tree is ready
                if event.isSet():
                    # Copy the buffer
                    buffer = worker_dict[i].copy_buffer()
                    # Clear the buffer
                    worker_dict[i].reset_buffer()
                    # Set the flag to false
                    event.clear()
                    # Add the new rewards in the master tree
                    self.integrate_buffer(buffer)

            # Test if all the workers are done
            if all(event.isSet() for event in finish_event_dict.values()):
                # End of the master thread
                stop = True

    def get_uct(self, worker_node):
        """
        Compute the master uct value of a worker node
        :param worker_node:
        :return:
        """
        # warning here it is a reference toward a worker node.
        node_hash = hash(tuple(worker_node.origins))

        master_node = self.nodes.get(node_hash, 0)
        if master_node == 0:
            # print("Node " + str(node_hash) + " is not in the master")
            return 0

        else:
            # print("Node " + str(node_hash) + " is in the master")
            uct_per_scenario = []
            for s, reward_per_scenario in enumerate(master_node.rewards):
                num_parent = 0
                uct_max_on_actions = 0

                for hist in master_node.parentNode.rewards[s]:
                    num_parent += sum(hist.h)

                num_node = sum(master_node.parentNode.rewards[s, A_DICT[master_node.arm]].h)

                if (num_parent == 0) or (num_node == 0):
                    uct_per_scenario.append(0)
                    continue

                exploration = UCT_COEFF * (2 * log(num_parent) / num_node) ** 0.5

                for hist in reward_per_scenario:
                    uct_value = hist.get_mean()

                    if uct_value > uct_max_on_actions:
                        uct_max_on_actions = uct_value

                uct_per_scenario.append(uct_max_on_actions + exploration)

            return np.dot(uct_per_scenario, self.probability)

    def get_children(self):
        """
        Add the children nodes to each master node
        :return:
        """
        nodes = dict(self.nodes)
        del nodes[hash(tuple([]))]  # remove the rootNode
        for node in nodes.values():
            node.parentNode.children.append(node)

    def get_depth(self):
        """
        Compute the depth of each master node and add it in their attributes
        :return:
        """
        node = self.nodes[hash(tuple([]))]
        list_nodes = [node]
        node.depth = 0
        while list_nodes:
            node = list_nodes.pop(0)
            for n in node.children:
                list_nodes.append(n)
                n.depth = node.depth + 1

        # get max depth of the tree
        self.max_depth = max(map(lambda i: self.nodes[i].depth, self.nodes))

    def get_best_policy(self):
        # Make sure all the variable have been computed
        if not self.nodes[hash(tuple([]))].children:
            self.get_children()
        if self.nodes[hash(tuple([]))].depth is None:
            self.get_depth()

        # get best global policy:
        print("Global policy")
        nodes_policy = [self.nodes[hash(tuple([]))]]  # rootNode
        policy = []
        node = nodes_policy[0]
        while node.children:
            child, action = self.get_best_child(node, idscenario=None)
            nodes_policy.append(child)
            policy.append(action)
            node = child
        self.best_global_policy = policy
        self.best_global_nodes_policy = nodes_policy

        # get best policy for each scenario:
        for id_scenario in range(len(self.Simulators)):
            print("Policy for scenario " + str(id_scenario))
            nodes_policy = [self.nodes[hash(tuple([]))]]  # rootNode
            policy = []
            node = nodes_policy[0]
            while node.children:
                child, action = self.get_best_child(node, idscenario=id_scenario)
                nodes_policy.append(child)
                policy.append(action)
                node = child
            self.best_policy[id_scenario] = policy
            self.best_nodes_policy[id_scenario] = nodes_policy

    def get_best_child(self, node, idscenario=None):
        best_reward = -1
        best_action = None
        best_child = None
        for child in node.children:
            reward_per_action = np.zeros(shape=len(ACTIONS))
            for j in range(len(ACTIONS)):
                if idscenario is None:
                    temp = np.zeros(shape=self.numScenarios)
                    for i in range(self.numScenarios):
                        temp[i] = child.rewards[i, j].get_mean()
                    reward_per_action[j] = np.dot(temp, self.probability)
                    # print(temp)
                    # print(self.probability)
                    # print(reward_per_action[j])
                else:
                    reward_per_action[j] = child.rewards[idscenario, j].get_mean()
            if np.max(reward_per_action) > best_reward:
                best_reward = np.max(reward_per_action)
                best_child = child
                best_action = child.arm
        print("best reward :" + str(best_reward) + " for action :" + str(best_action))
        return best_child, best_action

    def plot_tree(self, grey=False, idscenario=None):
        """
        Plot the master tree.

        :param boolean grey: if True => grey scale

        :param int idscenario: If not None, plot the corresponding worker tree
        """
        x0 = 0
        y0 = 0
        length = 1
        node = self.nodes[hash(tuple([]))]  # rootNode

        # Make sure all the variable have been computed
        if not node.children:
            self.get_children()
        if node.depth is None:
            self.get_depth()

        fig = plt.figure()
        ax = fig.add_subplot(1, 2, 1)

        if grey:
            self.plot_children(node, x0, y0, length, ax, idscenario=idscenario)
        else:
            self.plot_children(node, x0, y0, length, ax, 'k', idscenario=idscenario)

        ax.plot(0, 0, color="blue", marker='o', markersize='10')
        plt.axis('equal')
        fig.show()
        return fig, ax

    def plot_children(self, node, x, y, l, ax, color=None, idscenario=None):
        """
        Recursive function to plot the children of a master node.

        :param node:

        :param x:

        :param y:

        :param l:

        :param ax:

        :param color: if None => grayscale

        :param idscenario: if not None => plot only for one scenario

        :return: figure
        """
        x0 = x
        y0 = y
        for child in node.children:
            if idscenario is not None:
                if not child.is_expanded(idscenario):
                    continue
            x = x0 + l * sin(child.arm * pi / 180)
            y = y0 + l * cos(child.arm * pi / 180)
            if color is None:
                col = str((child.depth / self.max_depth) * 0.8)
            else:
                col = color

            ax.plot([x0, x], [y0, y], color=col, marker='o', markersize='6')
            # ax.annotate(str(child.depth), (x, y))
            self.plot_children(child, x, y, l, ax, color=color, idscenario=idscenario)

    def plot_best_policy(self, grey=False, idscenario=None):
        """
        Plot the master tree and its best policy
        :param grey:
        :param idscenario:
        :return:
        """
        # check if the best_policy has been computed
        if not self.best_global_policy:
            self.get_best_policy()

        # Get the right policy
        if idscenario is None:
            nodes_policy = self.best_global_nodes_policy
        else:
            nodes_policy = self.best_nodes_policy[idscenario]

        fig, ax = self.plot_tree(grey=grey, idscenario=idscenario)
        x0 = 0
        y0 = 0
        length = 1
        for node in nodes_policy[1:]:
            x = x0 + length * sin(node.arm * pi / 180)
            y = y0 + length * cos(node.arm * pi / 180)
            ax.plot([x0, x], [y0, y], color="red", marker='o', markersize='6')
            x0 = x
            y0 = y
        return fig, ax

    def plot_hist_best_policy(self, idscenario=None):
        # check if the best_policy has been computed
        if not self.best_global_policy:
            self.get_best_policy()

        # Get the right policy
        if idscenario is None:
            nodes_policy = self.best_global_nodes_policy
            policy = self.best_global_policy
        else:
            nodes_policy = self.best_nodes_policy[idscenario]
            policy = self.best_policy[idscenario]

        # Plot
        fig, ax1 = self.plot_best_policy(grey=True, idscenario=idscenario)
        ax2 = fig.add_subplot(1, 2, 2)
        # ax2.set_ylim([0, 30])
        barcollection = ax2.bar(x=Hist.MEANS, height=[0 for _ in Hist.MEANS],
                                width=Hist.THRESH[1] - Hist.THRESH[0])
        pt, = ax1.plot(0, 0, color="green", marker='o', markersize='7')
        x0, y0 = 0, 0
        x_list = [x0]
        y_list = [y0]
        for node in nodes_policy[1:]:
            x = x0 + 1 * sin(node.arm * pi / 180)
            y = y0 + 1 * cos(node.arm * pi / 180)
            x_list.append(x)
            y_list.append(y)
            x0, y0 = x, y

        print(len(policy))
        print(len(nodes_policy))

        def animate(i):
            n = nodes_policy[i]
            if i == len(nodes_policy) - 1:
                # last nodes: same reward for all actions
                a = 0
            else:
                a = A_DICT[policy[i]]
            if idscenario is None:
                hist = sum(n.rewards[ii, a].h * self.probability[ii] for ii in range(len(n.rewards[:, a])))
            else:
                hist = n.rewards[idscenario, a].h
            for j, b in enumerate(barcollection):
                b.set_height(hist[j])
            ax2.set_ylim([0, np.max(hist) + 1])
            pt.set_data(x_list[i], y_list[i])

            return barcollection, pt

        anim = animation.FuncAnimation(fig, animate, frames=len(nodes_policy), interval=1000, blit=False)
        plt.show()
        return fig

    def save_tree(self, name):
        filehandler = open("../data/" + name + '.pickle', 'wb')
        pickle.dump(self, filehandler)
        filehandler.close()

    @classmethod
    def load_tree(cls, name):
        filehandler = open("../data/" + name + '.pickle', 'rb')
        loaded_tree = pickle.load(filehandler)
        filehandler.close()
        return loaded_tree


class MasterNode:
    """
    Node of a MasterTree
    :ivar int hash:
    :ivar int action:
    :ivar MasterNode parentNode:
    :ivar numpy.array rewards: Array of `Hist`
    :ivar list children: List of children (MasterNode)
    :ivar int depth: Depth of the node
    """

    def __init__(self, numscenarios, nodehash=None, parentNode=None, action=None):
        self.hash = nodehash
        self.arm = action
        self.parentNode = parentNode
        self.rewards = np.array([[Hist() for _ in range(len(ACTIONS))] for _ in range(numscenarios)])
        self.children = []
        self.depth = None

    def add_reward(self, idscenario, reward):
        """
        Includes a reward into the histogram for all actions of one scenario.
        :param int idscenario: id of the scenario/workertree where the update is coming
        :param float reward: reward of the update
        """
        for hist in self.rewards[idscenario, :]:
            hist.add(reward)

    def add_reward_action(self, idscenario, action, reward):
        """
        Includes a reward into the histogram for one action of one scenario.
        :param idscenario:
        :param action:
        :param reward:
        :return:
        """
        self.rewards[idscenario, A_DICT[action]].add(reward)

    def backup(self, idscenario, reward):
        """
        Propagates the reward through the master tree.
        :param int idscenario: id of the scenario/workertree where the update is coming
        :param float reward: reward of the update
        """
        parent = self.parentNode
        if parent is not None:
            parent.add_reward_action(idscenario, self.arm, reward)
            parent.backup(idscenario, reward)

    def is_expanded(self, idscenario):
        """
        Check if this node has been expanded by the scenario `idscenario`
        :param idscenario:
        :return:
        """
        return not all(hist.is_empty() for hist in self.rewards[idscenario, :])

    def plot_hist(self, idscenario, action):
        # print(self.rewards[idscenario, action].h)
        fig, ax = plt.subplots()
        plt.bar(x=Hist.MEANS, height=self.rewards[idscenario, action].h, width=Hist.THRESH[1] - Hist.THRESH[0])
        fig.show()
        return fig

    def plot_mean_hist(self, action, probability):
        # Mean on all the scenarios:
        hist = sum(self.rewards[ii, action].h * probability[ii] for ii in range(len(self.rewards[:, action])))

        fig, ax = plt.subplots()
        # print(hist)
        plt.bar(x=Hist.MEANS, height=hist, width=Hist.THRESH[1] - Hist.THRESH[0])
        fig.show()
        return fig
